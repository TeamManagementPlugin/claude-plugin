#!/usr/bin/env python3
"""Tests for the optimize-protocol _func_* handlers (Phase 2).

Run with: python3 -m pytest test/test_protocol_engine_funcs.py -v
"""

import json
import math
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import patch, MagicMock

TEST_DIR = Path(__file__).parent
PROJECT_ROOT = TEST_DIR.parent
HOOKS_DIR = PROJECT_ROOT / "plugin" / "hooks"
sys.path.insert(0, str(HOOKS_DIR))


class FuncTestBase(TestCase):
    """Base fixture for optimize-func unit tests."""

    TASK_NAME = "o-test-funcs"
    BRANCH = "optimize/test-funcs"

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        (self.temp_dir / ".claude" / "state").mkdir(parents=True)
        (self.temp_dir / ".claude" / "state" / "protocol-logs").mkdir(parents=True)
        (self.temp_dir / "team-management" / "tasks" / self.TASK_NAME).mkdir(parents=True)
        (self.temp_dir / "team-management" / "protocol-configs" / "system").mkdir(parents=True)
        (self.temp_dir / "plugin" / "protocol-configs").mkdir(parents=True)

        self._write_json(self.temp_dir / ".claude" / "state" / "current_task.json", {
            "task": self.TASK_NAME,
            "branch": self.BRANCH,
            "services": [],
            "updated": "2026-05-05",
            "protocol": {
                "name": "optimize",
                "current_step": 3,
                "step_name": "experimentation",
                "started_at": "2026-05-05T00:00:00+00:00",
                "loop_iteration": 1,
                "experimentation_started_at": "2026-05-05T00:00:00+00:00",
            },
        })
        self._write_json(self.temp_dir / ".claude" / "state" / "daic-mode.json", {
            "mode": "implementation",
        })

        # Task file
        task_file = self.temp_dir / "team-management" / "tasks" / f"{self.TASK_NAME}.md"
        task_file.write_text(
            "---\n"
            f"task: {self.TASK_NAME}\n"
            f"branch: {self.BRANCH}\n"
            "status: in-progress\n"
            "created: 2026-05-05\n"
            "---\n\n"
            "# Test\n",
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

    def _read_json(self, path: Path) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _task_dir(self) -> Path:
        return self.temp_dir / "team-management" / "tasks" / self.TASK_NAME

    def _tsv_path(self) -> Path:
        return self._task_dir() / "results.tsv"

    def _write_optimize_state(self, **fields):
        from shared_state import write_optimize_state
        write_optimize_state(fields)


def _make_proc(returncode=0, stdout="", stderr=""):
    """Build a MagicMock that quacks like a CompletedProcess."""
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def _git_router(*, dirty=False, head="head1234", git_unavailable=False, dirty_stdout=None):
    """Return a side_effect router for protocol_engine.subprocess.run that
    handles the two git invocations log_experiment_result makes
    (`git status --porcelain` for the dirty-tree gate, then
    `git rev-parse --short HEAD` for the commit_sha fallback). Any other
    subprocess invocation raises — extend this router when log_experiment_result
    or any func patched into the same `subprocess.run` mock adds new
    invocations (otherwise tests fail with an opaque AssertionError pointing
    at this helper rather than the code under test).

    `dirty=True` → `git status --porcelain` returns " M tracked.py\\n" by default.
    `dirty_stdout=<str>` → use the supplied porcelain stdout verbatim (useful
    for testing engine-owned-path filtering with custom paths).
    `git_unavailable=True` → the router raises OSError on every call,
    simulating a non-repo / missing-git environment.
    """
    def _is_git_status(argv):
        return isinstance(argv, list) and "git" in argv[:1] and "status" in argv and "--porcelain" in argv

    def _is_git_rev_parse(argv):
        return isinstance(argv, list) and argv[:1] == ["git"] and "rev-parse" in argv

    def router(argv, **kwargs):
        if git_unavailable:
            raise OSError("git not available")
        if _is_git_status(argv):
            if dirty_stdout is not None:
                return _make_proc(returncode=0, stdout=dirty_stdout)
            return _make_proc(returncode=0, stdout=(" M tracked.py\n" if dirty else ""))
        if _is_git_rev_parse(argv):
            return _make_proc(returncode=0, stdout=f"{head}\n")
        raise AssertionError(f"Unexpected subprocess invocation: {argv}")
    return router


def _ok_metric_result(metric_value=42.5, run_count=1, wall_clock_s=2.5,
                     aggregator="median"):
    """Build a successful _func_run_metric return dict."""
    return {
        "func": "run_metric",
        "success": True,
        "metric_value": metric_value,
        "run_count": run_count,
        "wall_clock_s": wall_clock_s,
        "aggregator": aggregator,
        "raw_outputs": [{"stdout": str(metric_value), "stderr": "", "exit_code": 0}],
        "values": [metric_value],
    }


class TestLogExperimentResult(FuncTestBase):
    """_func_log_experiment_result: dirty-tree gate → run_metric → TSV append.

    The engine measures the metric on HEAD via _func_run_metric — LLM-passed
    metric_value/run_count/aggregator/wall_clock_s are NOT consulted (the
    engine is the single source of truth for the leaderboard). Tests mock
    _func_run_metric for predictable values and route subprocess.run via
    _git_router so both git invocations (status + rev-parse) are handled.
    """

    def test_appends_header_and_first_row(self):
        with patch.object(self.engine, "_func_run_metric",
                          return_value=_ok_metric_result()) as mock_metric:
            with patch("protocol_engine.subprocess.run",
                       side_effect=_git_router()):
                result = self.engine._func_log_experiment_result(args={
                    "commit_sha": "abc1234",
                    "hypothesis": "baseline",
                })
        self.assertTrue(result["success"])
        self.assertEqual(mock_metric.call_count, 1)

        tsv = self._tsv_path().read_text(encoding="utf-8")
        lines = tsv.strip().split("\n")
        self.assertEqual(len(lines), 2)  # header + 1 row
        self.assertIn("iteration", lines[0])
        self.assertIn("metric_value", lines[0])
        cols = lines[1].split("\t")
        self.assertEqual(cols[0], "1")  # loop_iteration from protocol state
        self.assertEqual(cols[2], "abc1234")
        self.assertEqual(cols[3], "42.5")
        self.assertEqual(cols[4], "1")  # run_count from run_metric
        self.assertEqual(cols[5], "median")  # aggregator from run_metric
        self.assertEqual(cols[7], "ok")
        self.assertEqual(cols[8], "baseline")

    def test_always_calls_run_metric(self):
        """Engine ALWAYS calls _func_run_metric — even when no metric args
        are passed. This is the load-bearing invariant: the engine is the
        single source of truth for the leaderboard."""
        with patch.object(self.engine, "_func_run_metric",
                          return_value=_ok_metric_result()) as mock_metric:
            with patch("protocol_engine.subprocess.run",
                       side_effect=_git_router()):
                self.engine._func_log_experiment_result(args={"hypothesis": "x"})
        self.assertEqual(mock_metric.call_count, 1)

    def test_llm_metric_value_arg_ignored(self):
        """LLM-passed metric_value MUST be ignored. The engine measures via
        _func_run_metric, full stop."""
        with patch.object(self.engine, "_func_run_metric",
                          return_value=_ok_metric_result(metric_value=42.5)):
            with patch("protocol_engine.subprocess.run",
                       side_effect=_git_router()):
                self.engine._func_log_experiment_result(args={
                    "hypothesis": "x",
                    "metric_value": 999.0,  # deprecated arg — must be ignored
                    "run_count": 999,
                    "wall_clock_s": 999.0,
                    "aggregator": "max",
                })
        cols = self._tsv_path().read_text(encoding="utf-8").strip().split("\n")[1].split("\t")
        self.assertEqual(cols[3], "42.5")  # not 999.0
        self.assertEqual(cols[4], "1")     # not 999
        self.assertEqual(cols[5], "median")  # not "max"

    def test_run_metric_failure_propagates(self):
        """run_metric subprocess crash / parser miss / timeout → propagate
        success=False with raw_outputs and stage='run_metric', no TSV row."""
        failure = {
            "func": "run_metric",
            "success": False,
            "error": "metric command exit 1 on run 1/3",
            "raw_outputs": [{"stdout": "", "stderr": "boom", "exit_code": 1}],
        }
        with patch.object(self.engine, "_func_run_metric", return_value=failure):
            with patch("protocol_engine.subprocess.run",
                       side_effect=_git_router()):
                result = self.engine._func_log_experiment_result(args={"hypothesis": "x"})
        self.assertFalse(result["success"])
        self.assertEqual(result["stage"], "run_metric")
        self.assertEqual(result["error"], "metric command exit 1 on run 1/3")
        self.assertIn("boom", result["raw_outputs"][0]["stderr"])
        self.assertFalse(self._tsv_path().exists())

    def test_dirty_tree_rejected(self):
        """Dirty tree → success=False, stage='dirty_tree_check', error
        suggests `iter<N>: <hypothesis>` commit message, no TSV row written,
        run_metric NOT called."""
        with patch.object(self.engine, "_func_run_metric") as mock_metric:
            with patch("protocol_engine.subprocess.run",
                       side_effect=_git_router(dirty=True)):
                result = self.engine._func_log_experiment_result(args={
                    "hypothesis": "newton-3rd-law",
                })
        self.assertFalse(result["success"])
        self.assertEqual(result["stage"], "dirty_tree_check")
        self.assertIn("M tracked.py", result["dirty_files"])
        # iteration is 1 from protocol state in fixture
        self.assertIn("iter1: newton-3rd-law", result["error"])
        self.assertFalse(self._tsv_path().exists())
        self.assertEqual(mock_metric.call_count, 0)

    def test_dirty_tree_uses_placeholder_when_no_hypothesis(self):
        """Dirty tree without hypothesis arg → suggested message uses
        '<your-hypothesis-label>' placeholder rather than crashing."""
        with patch("protocol_engine.subprocess.run",
                   side_effect=_git_router(dirty=True)):
            result = self.engine._func_log_experiment_result(args={})
        self.assertFalse(result["success"])
        self.assertIn("<your-hypothesis-label>", result["error"])

    def test_dirty_only_in_engine_owned_results_tsv_proceeds(self):
        """results.tsv is written by this very func — its modification on
        iteration N must NOT block iteration N+1's dirty-tree gate. Otherwise
        every multi-iteration optimize loop deadlocks after the first row."""
        engine_only = f" M team-management/tasks/{self.TASK_NAME}/results.tsv\n"
        with patch.object(self.engine, "_func_run_metric",
                          return_value=_ok_metric_result()) as mock_metric:
            with patch("protocol_engine.subprocess.run",
                       side_effect=_git_router(dirty_stdout=engine_only)):
                result = self.engine._func_log_experiment_result(args={"hypothesis": "x"})
        self.assertTrue(result["success"], result)
        self.assertEqual(mock_metric.call_count, 1)

    def test_dirty_only_in_engine_owned_task_md_proceeds(self):
        """update_best_commit writes optimize.best_commit / optimize.best_metric
        into the task .md frontmatter on improving iterations. That mutation
        must NOT block the next iteration's dirty-tree gate."""
        engine_only = f" M team-management/tasks/{self.TASK_NAME}.md\n"
        with patch.object(self.engine, "_func_run_metric",
                          return_value=_ok_metric_result()) as mock_metric:
            with patch("protocol_engine.subprocess.run",
                       side_effect=_git_router(dirty_stdout=engine_only)):
                result = self.engine._func_log_experiment_result(args={"hypothesis": "x"})
        self.assertTrue(result["success"], result)
        self.assertEqual(mock_metric.call_count, 1)

    def test_dirty_only_in_engine_owned_backup_proceeds(self):
        """The .results.tsv.bak rotation file (written every 100 rows) is
        also engine-owned and must not block the gate."""
        engine_only = f"?? team-management/tasks/{self.TASK_NAME}/.results.tsv.bak\n"
        with patch.object(self.engine, "_func_run_metric",
                          return_value=_ok_metric_result()):
            with patch("protocol_engine.subprocess.run",
                       side_effect=_git_router(dirty_stdout=engine_only)):
                result = self.engine._func_log_experiment_result(args={"hypothesis": "x"})
        self.assertTrue(result["success"], result)

    def test_dirty_in_user_code_blocks_even_with_engine_artifacts(self):
        """A user-code change MUST still block the gate, even when engine-owned
        artifacts are also dirty in the same iteration. The error's `dirty_files`
        field should list only the user-facing change (engine artifacts filtered)."""
        mixed = (
            f" M team-management/tasks/{self.TASK_NAME}/results.tsv\n"
            f" M team-management/tasks/{self.TASK_NAME}.md\n"
            " M backend/simulation.py\n"
        )
        with patch.object(self.engine, "_func_run_metric") as mock_metric:
            with patch("protocol_engine.subprocess.run",
                       side_effect=_git_router(dirty_stdout=mixed)):
                result = self.engine._func_log_experiment_result(args={
                    "hypothesis": "newton-3rd-law",
                })
        self.assertFalse(result["success"])
        self.assertEqual(result["stage"], "dirty_tree_check")
        self.assertEqual(mock_metric.call_count, 0)
        # dirty_files reports only the user-facing change
        self.assertIn("backend/simulation.py", result["dirty_files"])
        self.assertNotIn("results.tsv", result["dirty_files"])
        self.assertNotIn(f"{self.TASK_NAME}.md", result["dirty_files"])

    def test_dirty_in_similar_prefix_other_task_blocks(self):
        """Engine-owned filter must not over-match: a different task whose
        name shares a prefix with the active task must NOT be filtered out.
        E.g., if active task is `o-test-funcs`, then `o-test-funcs-other.md`
        is NOT engine-owned and must still block."""
        # Active task in fixture is "o-test-funcs"; create a sibling-name false positive
        other = " M team-management/tasks/o-test-funcs-other.md\n"
        with patch.object(self.engine, "_func_run_metric") as mock_metric:
            with patch("protocol_engine.subprocess.run",
                       side_effect=_git_router(dirty_stdout=other)):
                result = self.engine._func_log_experiment_result(args={"hypothesis": "x"})
        self.assertFalse(result["success"])
        self.assertEqual(result["stage"], "dirty_tree_check")
        self.assertEqual(mock_metric.call_count, 0)

    def test_user_authored_file_inside_task_dir_blocks(self):
        """User-authored files inside the task directory (metric.py, fixtures,
        etc.) MUST still block the gate — the engine-owned filter only exempts
        specific bookkeeping files, not the entire task namespace. Otherwise
        a user editing their metric script without committing would silently
        produce a TSV row whose commit_sha doesn't match the measured tree.
        Closes Codex Warning #1 on the broad-prefix filter."""
        user_file = f" M team-management/tasks/{self.TASK_NAME}/metric.py\n"
        with patch.object(self.engine, "_func_run_metric") as mock_metric:
            with patch("protocol_engine.subprocess.run",
                       side_effect=_git_router(dirty_stdout=user_file)):
                result = self.engine._func_log_experiment_result(args={"hypothesis": "x"})
        self.assertFalse(result["success"])
        self.assertEqual(result["stage"], "dirty_tree_check")
        self.assertIn("metric.py", result["dirty_files"])
        self.assertEqual(mock_metric.call_count, 0)

    def test_dirty_only_in_engine_owned_dir_task_readme_proceeds(self):
        """Dir-task .md files use `<task>/README.md` (canonical engine
        convention — see `_set_optimize_field` and 10+ other call sites).
        update_best_commit writes optimize.best_commit / optimize.best_metric
        to README.md on improving iterations; that mutation MUST NOT block
        the next iteration's dirty-tree gate. Closes the second-round
        regression flagged by both reviewers (`<task>/<task>.md` was the
        wrong allowlist entry; `README.md` is correct)."""
        engine_only = f" M team-management/tasks/{self.TASK_NAME}/README.md\n"
        with patch.object(self.engine, "_func_run_metric",
                          return_value=_ok_metric_result()) as mock_metric:
            with patch("protocol_engine.subprocess.run",
                       side_effect=_git_router(dirty_stdout=engine_only)):
                result = self.engine._func_log_experiment_result(args={"hypothesis": "x"})
        self.assertTrue(result["success"], result)
        self.assertEqual(mock_metric.call_count, 1)

    def test_bare_untracked_task_dir_proceeds(self):
        """When git reports an entirely-untracked task dir as a single
        collapsed entry `?? team-management/tasks/<task>/` (default git
        behaviour without --untracked-files=all), the bare-dir entry must
        be recognised as engine-owned. Otherwise iter 1 always fails the
        gate, because log_experiment_result's `task_dir.mkdir(...)` on
        iter 0 produces an untracked directory that git collapses into one
        line. Closes Codex Round-3 P2 warning."""
        # With trailing slash (canonical git format)
        bare = f"?? team-management/tasks/{self.TASK_NAME}/\n"
        with patch.object(self.engine, "_func_run_metric",
                          return_value=_ok_metric_result()) as mock_metric:
            with patch("protocol_engine.subprocess.run",
                       side_effect=_git_router(dirty_stdout=bare)):
                result = self.engine._func_log_experiment_result(args={"hypothesis": "x"})
        self.assertTrue(result["success"], result)
        self.assertEqual(mock_metric.call_count, 1)

        # Without trailing slash (defensive — some git versions / configs)
        if self._tsv_path().exists():
            self._tsv_path().unlink()
        bare_no_slash = f"?? team-management/tasks/{self.TASK_NAME}\n"
        with patch.object(self.engine, "_func_run_metric",
                          return_value=_ok_metric_result()) as mock_metric:
            with patch("protocol_engine.subprocess.run",
                       side_effect=_git_router(dirty_stdout=bare_no_slash)):
                result = self.engine._func_log_experiment_result(args={"hypothesis": "x"})
        self.assertTrue(result["success"], result)
        self.assertEqual(mock_metric.call_count, 1)

    def test_rename_user_to_engine_path_blocks(self):
        """A user file renamed INTO an engine-owned name (e.g.
        `git mv src/foo.py team-management/tasks/<task>/results.tsv`) MUST
        block the gate. The deletion from src/ is a real user-side change.
        Without checking BOTH paths of a rename, the line would be filtered
        because only the destination is engine-owned. Closes Codex Round-4
        Warning #1."""
        rename_line = (
            f"R  src/foo.py -> team-management/tasks/{self.TASK_NAME}/results.tsv\n"
        )
        with patch.object(self.engine, "_func_run_metric") as mock_metric:
            with patch("protocol_engine.subprocess.run",
                       side_effect=_git_router(dirty_stdout=rename_line)):
                result = self.engine._func_log_experiment_result(args={"hypothesis": "x"})
        self.assertFalse(result["success"])
        self.assertEqual(result["stage"], "dirty_tree_check")
        self.assertEqual(mock_metric.call_count, 0)

    def test_rename_engine_to_user_path_blocks(self):
        """The mirror case: an engine-owned file renamed OUT to a user path
        is also a user-side change (the user moved the engine artifact
        somewhere it doesn't belong). Both directions must block."""
        rename_line = (
            f"R  team-management/tasks/{self.TASK_NAME}/results.tsv -> src/new.py\n"
        )
        with patch.object(self.engine, "_func_run_metric") as mock_metric:
            with patch("protocol_engine.subprocess.run",
                       side_effect=_git_router(dirty_stdout=rename_line)):
                result = self.engine._func_log_experiment_result(args={"hypothesis": "x"})
        self.assertFalse(result["success"])
        self.assertEqual(result["stage"], "dirty_tree_check")
        self.assertEqual(mock_metric.call_count, 0)

    def test_rename_engine_to_engine_filters(self):
        """Engine-internal rename (e.g. results.tsv → results.tsv.run-1
        as part of restart archive) MUST be filtered — both paths are
        engine-owned. Otherwise restarts would falsely block the next
        iteration."""
        rename_line = (
            f"R  team-management/tasks/{self.TASK_NAME}/results.tsv "
            f"-> team-management/tasks/{self.TASK_NAME}/results.tsv.run-1\n"
        )
        with patch.object(self.engine, "_func_run_metric",
                          return_value=_ok_metric_result()) as mock_metric:
            with patch("protocol_engine.subprocess.run",
                       side_effect=_git_router(dirty_stdout=rename_line)):
                result = self.engine._func_log_experiment_result(args={"hypothesis": "x"})
        self.assertTrue(result["success"], result)
        self.assertEqual(mock_metric.call_count, 1)

    def test_commit_sha_captured_before_run_metric(self):
        """The SHA must be resolved BEFORE _func_run_metric runs. If a metric
        script mutates HEAD (e.g. `git checkout <other-rev>`, post-run commit),
        capturing the SHA after run_metric would record the wrong revision —
        breaking the "row metric value matches recorded SHA" invariant.
        Closes Codex Round-4 Warning #2."""
        call_order = []

        def metric_mock():
            call_order.append("run_metric")
            return _ok_metric_result()

        def status_router(argv, **kwargs):
            if isinstance(argv, list) and "status" in argv and "--porcelain" in argv:
                call_order.append("git_status")
                return _make_proc(returncode=0, stdout="")
            if isinstance(argv, list) and "rev-parse" in argv:
                call_order.append("git_rev_parse")
                return _make_proc(returncode=0, stdout="pre1234\n")
            raise AssertionError(f"unexpected: {argv}")

        with patch.object(self.engine, "_func_run_metric",
                          side_effect=metric_mock):
            with patch("protocol_engine.subprocess.run",
                       side_effect=status_router):
                result = self.engine._func_log_experiment_result(args={"hypothesis": "x"})

        self.assertTrue(result["success"], result)
        # Ordering invariant: dirty-tree status → rev-parse → run_metric
        self.assertEqual(call_order[0], "git_status")
        self.assertIn("git_rev_parse", call_order)
        self.assertIn("run_metric", call_order)
        rp = call_order.index("git_rev_parse")
        rm = call_order.index("run_metric")
        self.assertLess(rp, rm,
                        f"git_rev_parse must run before _func_run_metric, got: {call_order}")
        # TSV recorded the pre-resolved SHA
        cols = self._tsv_path().read_text(encoding="utf-8").strip().split("\n")[1].split("\t")
        self.assertEqual(cols[2], "pre1234")

    def test_git_status_uses_untracked_files_all_flag(self):
        """log_experiment_result must invoke `git status --porcelain
        --untracked-files=all` so newly-created untracked files inside the
        task dir get expanded per-file (not collapsed to a bare-dir entry).
        Belt-and-braces with the bare-dir handling above — without --uall,
        the helper would fall back to bare-dir filtering, but having both
        gives a defence in depth."""
        captured_argvs = []

        def router(argv, **kwargs):
            captured_argvs.append(list(argv) if isinstance(argv, list) else argv)
            if isinstance(argv, list) and "status" in argv and "--porcelain" in argv:
                return _make_proc(returncode=0, stdout="")
            if isinstance(argv, list) and "rev-parse" in argv:
                return _make_proc(returncode=0, stdout="head1234\n")
            raise AssertionError(f"Unexpected: {argv}")

        with patch.object(self.engine, "_func_run_metric",
                          return_value=_ok_metric_result()):
            with patch("protocol_engine.subprocess.run", side_effect=router):
                self.engine._func_log_experiment_result(args={"hypothesis": "x"})

        status_calls = [a for a in captured_argvs
                        if isinstance(a, list) and "status" in a and "--porcelain" in a]
        self.assertEqual(len(status_calls), 1)
        self.assertIn("--untracked-files=all", status_calls[0])
        # Also asserts the core.quotePath=false flag is passed (Codex Round-5 fix
        # for Unicode task names — git's default C-quoting would break the
        # per-file allowlist for non-ASCII paths).
        self.assertIn("core.quotePath=false", status_calls[0])

    def test_filter_engine_owned_dirty_lines_helper(self):
        """Direct unit coverage of the static helper — exercises edge cases
        (rename arrows, quoted paths, sibling-name false positives, empty
        lines, blank stdout, run-archive variants, resume bookkeeping,
        user-authored files inside task dir)."""
        f = self.engine._filter_engine_owned_dirty_lines
        task = "o-test-funcs"

        # Empty stdout → no relevant lines
        self.assertEqual(f("", task), [])
        # Only engine-owned → empty
        self.assertEqual(f(
            f" M team-management/tasks/{task}.md\n"
            f" M team-management/tasks/{task}/results.tsv\n"
            f"?? team-management/tasks/{task}/.results.tsv.bak\n"
            f" M team-management/tasks/{task}/results.tsv.run-1\n"
            f"?? team-management/tasks/{task}/results.tsv.run-12\n"
            f" M team-management/tasks/{task}/resume-stdout-tail.txt\n"
            f" M team-management/tasks/{task}/resume-blocked.txt\n"
            f" M team-management/tasks/{task}/README.md\n",  # dir-mode .md (canonical)
            task), [])
        # Mixed → only user line surfaces
        rel = f(
            f" M team-management/tasks/{task}/results.tsv\n"
            " M backend/sim.py\n",
            task)
        self.assertEqual(rel, [" M backend/sim.py"])
        # Sibling-name false positive — must NOT be filtered
        rel = f(" M team-management/tasks/o-test-funcs-other.md\n", task)
        self.assertEqual(len(rel), 1)
        # Rename user → engine path: new contract requires BOTH paths
        # engine-owned, so the deletion of src/old.py is a real change → BLOCKS.
        rel = f(f"R  src/old.py -> team-management/tasks/{task}/results.tsv\n", task)
        self.assertEqual(len(rel), 1)
        # Rename engine → user path: also a user change → blocks
        rel = f(f"R  team-management/tasks/{task}/results.tsv -> src/new.py\n", task)
        self.assertEqual(len(rel), 1)
        # Rename engine → engine (e.g. results.tsv → results.tsv.run-1 archive) → filtered
        rel = f(
            f"R  team-management/tasks/{task}/results.tsv "
            f"-> team-management/tasks/{task}/results.tsv.run-1\n",
            task)
        self.assertEqual(rel, [])
        # Quoted path (special chars) under task dir → filtered
        rel = f(f' M "team-management/tasks/{task}/results.tsv"\n', task)
        self.assertEqual(rel, [])
        # User-authored file inside task dir — NOT filtered (Codex Warning #1)
        rel = f(f" M team-management/tasks/{task}/metric.py\n", task)
        self.assertEqual(len(rel), 1)
        rel = f(f"?? team-management/tasks/{task}/fixture.json\n", task)
        self.assertEqual(len(rel), 1)
        # File whose name happens to start with "results.tsv" prefix but isn't
        # the actual archive pattern — e.g., "results.tsv.html" → still filtered
        # because it matches startswith("results.tsv.run-") only for run-archives,
        # and a user wouldn't normally create a non-run-archive file matching
        # "results.tsv.<anything>". But user-created "results.tsv.bak" without
        # the leading dot would NOT match (".results.tsv.bak" only).
        rel = f(f" M team-management/tasks/{task}/results.tsv.user-export\n", task)
        self.assertEqual(len(rel), 1)  # Not a run archive, blocks correctly
        rel = f(f" M team-management/tasks/{task}/results.tsv.bak\n", task)
        self.assertEqual(len(rel), 1)  # Note: leading-dot variant is filtered, this isn't

    def test_clean_tree_proceeds_to_run_metric(self):
        """Clean tree → run_metric called → TSV row written."""
        with patch.object(self.engine, "_func_run_metric",
                          return_value=_ok_metric_result()) as mock_metric:
            with patch("protocol_engine.subprocess.run",
                       side_effect=_git_router(dirty=False)):
                result = self.engine._func_log_experiment_result(args={"hypothesis": "x"})
        self.assertTrue(result["success"])
        self.assertEqual(mock_metric.call_count, 1)
        self.assertTrue(self._tsv_path().exists())

    def test_git_unavailable_skips_dirty_check(self):
        """Git unavailable (non-repo / OSError on git status) → dirty-tree
        check is skipped defensively, flow continues to run_metric."""
        # Router raises OSError on every git invocation — both `git status`
        # and `git rev-parse`. log_experiment_result must swallow both and
        # write the row with commit_sha='-'.
        with patch.object(self.engine, "_func_run_metric",
                          return_value=_ok_metric_result()):
            with patch("protocol_engine.subprocess.run",
                       side_effect=_git_router(git_unavailable=True)):
                result = self.engine._func_log_experiment_result(args={"hypothesis": "x"})
        self.assertTrue(result["success"])
        cols = self._tsv_path().read_text(encoding="utf-8").strip().split("\n")[1].split("\t")
        self.assertEqual(cols[2], "-")

    def test_run_metric_returns_nan_records_crash_row(self):
        """Defensive backstop: if run_metric ever returns NaN/inf
        (shouldn't happen — its contract is a valid float), the write site
        still records a `crash` row rather than corrupting the TSV with the
        literal NaN/inf token."""
        for bad_value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=bad_value):
                if self._tsv_path().exists():
                    self._tsv_path().unlink()
                with patch.object(self.engine, "_func_run_metric",
                                  return_value=_ok_metric_result(metric_value=bad_value)):
                    with patch("protocol_engine.subprocess.run",
                               side_effect=_git_router()):
                        result = self.engine._func_log_experiment_result(args={"hypothesis": "edge"})
                self.assertTrue(result["success"])
                cols = self._tsv_path().read_text(encoding="utf-8").strip().split("\n")[1].split("\t")
                self.assertEqual(cols[7], "crash")

    def test_run_metric_returns_non_float_records_crash_row(self):
        """Defensive backstop: if run_metric ever returns a non-float
        value (shouldn't happen), float() coercion fails → crash row."""
        with patch.object(self.engine, "_func_run_metric",
                          return_value=_ok_metric_result(metric_value="not-a-number")):
            with patch("protocol_engine.subprocess.run",
                       side_effect=_git_router()):
                result = self.engine._func_log_experiment_result(args={"hypothesis": "broken"})
        self.assertTrue(result["success"])
        cols = self._tsv_path().read_text(encoding="utf-8").strip().split("\n")[1].split("\t")
        self.assertEqual(cols[3], "NaN")
        self.assertEqual(cols[7], "crash")

    def test_tab_and_newline_in_hypothesis_stripped(self):
        with patch.object(self.engine, "_func_run_metric",
                          return_value=_ok_metric_result()):
            with patch("protocol_engine.subprocess.run",
                       side_effect=_git_router()):
                result = self.engine._func_log_experiment_result(args={
                    "hypothesis": "line1\twith tab\nand newline",
                })
        self.assertTrue(result["success"])
        tsv = self._tsv_path().read_text(encoding="utf-8")
        lines = tsv.strip().split("\n")
        # Row count should be 2 (header + 1) — newline NOT split into a new row
        self.assertEqual(len(lines), 2)
        # No tab inside the hypothesis cell (would corrupt column layout)
        cols = lines[1].split("\t")
        self.assertEqual(len(cols), 9)

    def test_backup_rotation_at_100_rows(self):
        # Pre-populate TSV with 99 rows (header + 99 data rows)
        header = "iteration\ttimestamp\tcommit_sha\tmetric_value\trun_count\taggregator\twall_clock_s\tstatus\thypothesis\n"
        body = "\n".join(f"{i}\t2026-05-05T00:00\t-\t{i}.0\t1\tmedian\t0.1\tok\trow{i}" for i in range(99))
        self._tsv_path().write_text(header + body + "\n", encoding="utf-8")

        # Append the 100th data row → should trigger backup rotation
        with patch.object(self.engine, "_func_run_metric",
                          return_value=_ok_metric_result(metric_value=100.0)):
            with patch("protocol_engine.subprocess.run",
                       side_effect=_git_router()):
                result = self.engine._func_log_experiment_result(args={
                    "hypothesis": "trigger backup",
                })
        self.assertTrue(result["success"])
        backup = self._task_dir() / ".results.tsv.bak"
        self.assertTrue(backup.exists(), "backup .results.tsv.bak should exist after row 100")

    def test_summary_row_uses_iteration_minus_1(self):
        with patch.object(self.engine, "_func_run_metric",
                          return_value=_ok_metric_result(metric_value=0.0,
                                                         run_count=0,
                                                         wall_clock_s=0.0)):
            with patch("protocol_engine.subprocess.run",
                       side_effect=_git_router()):
                result = self.engine._func_log_experiment_result(args={
                    "commit_sha": "-",
                    "hypothesis": "max_iterations terminator",
                    "status": "summary",
                    "iteration_override": -1,
                })
        self.assertTrue(result["success"])
        tsv = self._tsv_path().read_text(encoding="utf-8")
        cols = tsv.strip().split("\n")[1].split("\t")
        self.assertEqual(cols[0], "-1")
        self.assertEqual(cols[7], "summary")

    def test_falls_back_to_git_head_when_commit_sha_omitted(self):
        """No commit_sha arg → git rev-parse --short HEAD fallback."""
        with patch.object(self.engine, "_func_run_metric",
                          return_value=_ok_metric_result(metric_value=1.5)):
            with patch("protocol_engine.subprocess.run",
                       side_effect=_git_router(head="head1234")):
                result = self.engine._func_log_experiment_result(args={
                    "hypothesis": "no commit_sha arg",
                })
        self.assertTrue(result["success"])
        cols = self._tsv_path().read_text(encoding="utf-8").strip().split("\n")[1].split("\t")
        self.assertEqual(cols[2], "head1234")

    def test_falls_back_when_commit_sha_is_dash(self):
        """commit_sha='-' is treated as missing → git rev-parse fallback."""
        with patch.object(self.engine, "_func_run_metric",
                          return_value=_ok_metric_result(metric_value=1.5)):
            with patch("protocol_engine.subprocess.run",
                       side_effect=_git_router(head="head1234")):
                result = self.engine._func_log_experiment_result(args={
                    "commit_sha": "-",
                    "hypothesis": "explicit dash",
                })
        self.assertTrue(result["success"])
        cols = self._tsv_path().read_text(encoding="utf-8").strip().split("\n")[1].split("\t")
        self.assertEqual(cols[2], "head1234")

    def test_falls_back_when_commit_sha_is_empty_string(self):
        """commit_sha='' is treated as missing → git rev-parse fallback."""
        with patch.object(self.engine, "_func_run_metric",
                          return_value=_ok_metric_result(metric_value=1.5)):
            with patch("protocol_engine.subprocess.run",
                       side_effect=_git_router(head="head1234")):
                result = self.engine._func_log_experiment_result(args={
                    "commit_sha": "",
                    "hypothesis": "empty string",
                })
        self.assertTrue(result["success"])
        cols = self._tsv_path().read_text(encoding="utf-8").strip().split("\n")[1].split("\t")
        self.assertEqual(cols[2], "head1234")

    def test_explicit_commit_sha_overrides_git_fallback(self):
        """Explicit commit_sha arg takes priority — git rev-parse for SHA
        must NOT be called (only `git status` for the dirty-tree gate)."""
        seen_argvs = []

        def router(argv, **kwargs):
            seen_argvs.append(argv)
            if isinstance(argv, list) and "status" in argv and "--porcelain" in argv:
                return _make_proc(returncode=0, stdout="")
            raise AssertionError(f"Unexpected subprocess: {argv}")

        with patch.object(self.engine, "_func_run_metric",
                          return_value=_ok_metric_result(metric_value=1.5)):
            with patch("protocol_engine.subprocess.run", side_effect=router):
                result = self.engine._func_log_experiment_result(args={
                    "commit_sha": "explicit",
                    "hypothesis": "explicit override",
                })
        self.assertTrue(result["success"])
        # Only `git status --porcelain` invoked, no `git rev-parse`
        self.assertEqual(len(seen_argvs), 1)
        self.assertIn("status", seen_argvs[0])
        self.assertIn("--porcelain", seen_argvs[0])
        self.assertNotIn("rev-parse", seen_argvs[0])
        cols = self._tsv_path().read_text(encoding="utf-8").strip().split("\n")[1].split("\t")
        self.assertEqual(cols[2], "explicit")


class TestRunMetric(FuncTestBase):
    """_func_run_metric: subprocess + env filter + N-runs averaging."""

    def setUp(self):
        super().setUp()
        self._write_optimize_state(
            metric_command="python -c 'print(42.5)'",
            metric_parser=r"^([+-]?\d+\.?\d*(?:[eE][+-]?\d+)?)\s*$",
            runs_per_iteration=1,
            aggregator="median",
            env_pass=[],
        )

    def _make_completed(self, returncode=0, stdout="42.5\n", stderr=""):
        m = MagicMock()
        m.returncode = returncode
        m.stdout = stdout
        m.stderr = stderr
        return m

    def test_single_run_returns_metric(self):
        with patch("protocol_engine.subprocess.run", return_value=self._make_completed()) as mock_run:
            result = self.engine._func_run_metric()
            self.assertTrue(result["success"])
            self.assertEqual(result["metric_value"], 42.5)
            self.assertEqual(result["run_count"], 1)
            self.assertEqual(mock_run.call_count, 1)

    def test_multi_run_median_aggregator(self):
        self._write_optimize_state(
            metric_command="python -c 'print(0)'",
            metric_parser=r"^([+-]?\d+\.?\d*)\s*$",
            runs_per_iteration=3,
            aggregator="median",
            env_pass=[],
        )
        outputs = ["1.0\n", "2.0\n", "3.0\n"]
        with patch("protocol_engine.subprocess.run", side_effect=[self._make_completed(stdout=o) for o in outputs]):
            result = self.engine._func_run_metric()
            self.assertTrue(result["success"])
            self.assertEqual(result["metric_value"], 2.0)
            self.assertEqual(result["run_count"], 3)

    def test_multi_run_mean_aggregator(self):
        self._write_optimize_state(
            metric_command="python -c 'print(0)'",
            metric_parser=r"^([+-]?\d+\.?\d*)\s*$",
            runs_per_iteration=4,
            aggregator="mean",
            env_pass=[],
        )
        outputs = ["1.0\n", "2.0\n", "3.0\n", "10.0\n"]  # mean = 4.0
        with patch("protocol_engine.subprocess.run", side_effect=[self._make_completed(stdout=o) for o in outputs]):
            result = self.engine._func_run_metric()
            self.assertEqual(result["metric_value"], 4.0)

    def test_credential_env_vars_stripped(self):
        captured_env = {}

        def capture(*args, **kwargs):
            captured_env.update(kwargs.get("env") or {})
            return self._make_completed()

        # Simulate an environment with a credential-shaped key
        with patch("protocol_engine.os.environ", {"PATH": "/usr/bin", "AWS_SECRET_KEY": "AKIA-leak", "MY_TOKEN": "xyz", "USER": "u"}):
            with patch("protocol_engine.subprocess.run", side_effect=capture):
                result = self.engine._func_run_metric()
                self.assertTrue(result["success"])
                self.assertNotIn("AWS_SECRET_KEY", captured_env)
                self.assertNotIn("MY_TOKEN", captured_env)
                self.assertIn("PATH", captured_env)

    def test_env_pass_extends_allowlist(self):
        captured_env = {}

        def capture(*args, **kwargs):
            captured_env.update(kwargs.get("env") or {})
            return self._make_completed()

        self._write_optimize_state(
            metric_command="python -c 'print(0)'",
            metric_parser=r"^([+-]?\d+\.?\d*)\s*$",
            runs_per_iteration=1,
            aggregator="median",
            env_pass=["MY_CUSTOM_VAR"],
        )
        with patch("protocol_engine.os.environ", {"PATH": "/usr/bin", "MY_CUSTOM_VAR": "value", "RANDOM_VAR": "ignored"}):
            with patch("protocol_engine.subprocess.run", side_effect=capture):
                self.engine._func_run_metric()
                self.assertEqual(captured_env.get("MY_CUSTOM_VAR"), "value")
                self.assertNotIn("RANDOM_VAR", captured_env)

    def test_metacharacter_in_command_blocks_run(self):
        self._write_optimize_state(
            metric_command="python -c 'print(0)' ; rm -rf /",
            metric_parser=r".*",
            runs_per_iteration=1,
            aggregator="median",
            env_pass=[],
        )
        result = self.engine._func_run_metric()
        self.assertFalse(result["success"])
        self.assertIn("forbidden metacharacter", result["error"])

    def test_non_zero_exit_returns_failure(self):
        with patch("protocol_engine.subprocess.run", return_value=self._make_completed(returncode=1, stdout="", stderr="boom")):
            result = self.engine._func_run_metric()
            self.assertFalse(result["success"])
            self.assertIn("exit", result["error"].lower())

    def test_unparseable_stdout_returns_failure(self):
        with patch("protocol_engine.subprocess.run", return_value=self._make_completed(stdout="no number here")):
            result = self.engine._func_run_metric()
            self.assertFalse(result["success"])
            self.assertIn("parse", result["error"].lower())

    def test_catastrophic_parser_times_out_fast(self):
        """R2-O2: a catastrophic-backtracking metric_parser must fail fast via
        the killable child-process timeout instead of hanging the engine
        process (a daemon-thread timeout cannot work — stdlib re holds the
        GIL for the entire match)."""
        import optimize_completion
        self._write_optimize_state(
            metric_command="python -c 'print(0)'",
            metric_parser=r"(a+)+b",  # classic exponential backtracking
            runs_per_iteration=1,
            aggregator="median",
            env_pass=[],
        )
        evil_stdout = "a" * 5000 + "x"
        start = time.monotonic()
        with patch.object(optimize_completion, "METRIC_PARSER_TIMEOUT_S", 0.5), \
             patch("protocol_engine.subprocess.run", return_value=self._make_completed(stdout=evil_stdout)):
            result = self.engine._func_run_metric()
        elapsed = time.monotonic() - start
        self.assertFalse(result["success"])
        self.assertIn("timed out", result["error"])
        self.assertLess(elapsed, 5.0)

    def test_oversized_stdout_parses_when_match_within_cap(self):
        """R2-O2: stdout longer than the search cap still parses when the
        metric line appears within the capped prefix."""
        import optimize_completion
        big_stdout = "42.5\n" + "x" * (optimize_completion.METRIC_STDOUT_SEARCH_CAP + 1000)
        with patch("protocol_engine.subprocess.run", return_value=self._make_completed(stdout=big_stdout)):
            result = self.engine._func_run_metric()
        self.assertTrue(result["success"])
        self.assertEqual(result["metric_value"], 42.5)

    def test_match_beyond_cap_returns_clear_error(self):
        """R2-O2: when the metric line appears only BEYOND the search cap, the
        error must say the search was truncated rather than a generic parse
        failure."""
        import optimize_completion
        big_stdout = "x" * (optimize_completion.METRIC_STDOUT_SEARCH_CAP + 10) + "\n42.5\n"
        with patch("protocol_engine.subprocess.run", return_value=self._make_completed(stdout=big_stdout)):
            result = self.engine._func_run_metric()
        self.assertFalse(result["success"])
        self.assertIn("truncated", result["error"])

    def test_metric_line_straddling_cap_errors_instead_of_partial_parse(self):
        """R2-O2 (review warning): a metric line cut mid-number by the cap
        must NOT silently parse the truncated prefix (42.567 → 42.5) — the
        partial trailing line is dropped and the truncated-stdout error
        fires instead."""
        import optimize_completion
        cap = optimize_completion.METRIC_STDOUT_SEARCH_CAP
        # capped text ends exactly with "\n42.5" — a clean-parsing prefix of 42.567
        big_stdout = "x" * (cap - 5) + "\n42.567\n"
        with patch("protocol_engine.subprocess.run", return_value=self._make_completed(stdout=big_stdout)):
            result = self.engine._func_run_metric()
        self.assertFalse(result["success"])
        self.assertIn("truncated", result["error"])


class TestValidateMetricScript(FuncTestBase):
    """_func_validate_metric_script: 2-run stability check."""

    def setUp(self):
        super().setUp()
        self._write_optimize_state(
            metric_command="python -c 'print(0)'",
            metric_parser=r"^([+-]?\d+\.?\d*)\s*$",
            runs_per_iteration=1,
            aggregator="median",
            stability_threshold_pct=5.0,
            env_pass=[],
        )

    def _make_completed(self, returncode=0, stdout="0\n"):
        m = MagicMock()
        m.returncode = returncode
        m.stdout = stdout
        m.stderr = ""
        return m

    def test_two_stable_runs_succeed(self):
        runs = [self._make_completed(stdout="10.0\n"),
                self._make_completed(stdout="10.2\n")]  # 2% delta
        with patch("protocol_engine.subprocess.run", side_effect=runs):
            result = self.engine._func_validate_metric_script()
            self.assertTrue(result["success"])
            self.assertEqual(result["first"], 10.0)
            self.assertEqual(result["second"], 10.2)
            self.assertLess(result["delta_pct"], 5.0)

    def test_unstable_runs_fail(self):
        runs = [self._make_completed(stdout="10.0\n"),
                self._make_completed(stdout="12.0\n")]  # 20% delta
        with patch("protocol_engine.subprocess.run", side_effect=runs):
            result = self.engine._func_validate_metric_script()
            self.assertFalse(result["success"])
            self.assertIn("instability", result["error"])
            self.assertGreater(result["delta_pct"], 5.0)

    def test_first_run_failure_propagates(self):
        runs = [self._make_completed(returncode=1, stdout="")]
        with patch("protocol_engine.subprocess.run", side_effect=runs):
            result = self.engine._func_validate_metric_script()
            self.assertFalse(result["success"])
            self.assertIn("first validation run failed", result["error"])

    def test_custom_threshold_allows_wider_drift(self):
        self._write_optimize_state(
            metric_command="python -c 'print(0)'",
            metric_parser=r"^([+-]?\d+\.?\d*)\s*$",
            runs_per_iteration=1,
            aggregator="median",
            stability_threshold_pct=25.0,  # widen
            env_pass=[],
        )
        runs = [self._make_completed(stdout="10.0\n"),
                self._make_completed(stdout="12.0\n")]  # 20% — within 25% now
        with patch("protocol_engine.subprocess.run", side_effect=runs):
            result = self.engine._func_validate_metric_script()
            self.assertTrue(result["success"])

    def test_both_zero_runs_stable(self):
        """R2-O3: two runs both returning 0 are perfectly stable (delta 0%),
        not 'infinitely unstable'."""
        runs = [self._make_completed(stdout="0\n"),
                self._make_completed(stdout="0\n")]
        with patch("protocol_engine.subprocess.run", side_effect=runs):
            result = self.engine._func_validate_metric_script()
            self.assertTrue(result["success"])
            self.assertEqual(result["delta_pct"], 0.0)

    def test_zero_then_nonzero_fails_with_zero_hint_not_inf(self):
        """R2-O3: v1=0, v2!=0 must yield a symmetric 100% delta (not inf) and
        a zero-run hint distinct from the generic instability message."""
        runs = [self._make_completed(stdout="0\n"),
                self._make_completed(stdout="5.0\n")]
        with patch("protocol_engine.subprocess.run", side_effect=runs):
            result = self.engine._func_validate_metric_script()
            self.assertFalse(result["success"])
            self.assertEqual(result["delta_pct"], 100.0)
            self.assertIn("returned 0", result["error"])

    def test_nonzero_then_zero_symmetric(self):
        """R2-O3: the mirror order v1!=0, v2=0 yields the same 100% delta and
        the same zero-run hint — the gate is symmetric at zero."""
        runs = [self._make_completed(stdout="5.0\n"),
                self._make_completed(stdout="0\n")]
        with patch("protocol_engine.subprocess.run", side_effect=runs):
            result = self.engine._func_validate_metric_script()
            self.assertFalse(result["success"])
            self.assertEqual(result["delta_pct"], 100.0)
            self.assertIn("returned 0", result["error"])


class TestCaptureMetricBaseline(FuncTestBase):
    """_func_capture_metric_baseline: run once, persist baseline metric +
    timing into optimize-state.json + baseline_commit into task frontmatter."""

    def setUp(self):
        super().setUp()
        self._write_optimize_state(
            metric_command="python -c 'print(0)'",
            metric_parser=r"^([+-]?\d+\.?\d*)\s*$",
            runs_per_iteration=1,
            aggregator="median",
            env_pass=[],
        )

    def _make_completed(self, returncode=0, stdout="42.0\n"):
        m = MagicMock()
        m.returncode = returncode
        m.stdout = stdout
        m.stderr = ""
        return m

    def test_records_baseline_metric_and_timing(self):
        # First subprocess.run call is metric, subsequent is git rev-parse
        metric_run = self._make_completed(stdout="42.0\n")
        git_run = MagicMock()
        git_run.returncode = 0
        git_run.stdout = "abc1234\n"
        git_run.stderr = ""
        with patch("protocol_engine.subprocess.run", side_effect=[metric_run, git_run]):
            result = self.engine._func_capture_metric_baseline()
            self.assertTrue(result["success"])
            self.assertEqual(result["baseline_metric"], 42.0)
            self.assertEqual(result["baseline_commit"], "abc1234")

        # optimize-state.json updated
        from shared_state import OPTIMIZE_STATE_FILE
        state = self._read_json(OPTIMIZE_STATE_FILE)
        self.assertEqual(state["baseline_metric"], 42.0)
        self.assertEqual(state["baseline_commit"], "abc1234")
        self.assertIn("baseline_wall_clock_s", state)

    def test_writes_baseline_commit_to_frontmatter(self):
        metric_run = self._make_completed(stdout="7.0\n")
        git_run = MagicMock()
        git_run.returncode = 0
        git_run.stdout = "deadbee\n"
        git_run.stderr = ""
        with patch("protocol_engine.subprocess.run", side_effect=[metric_run, git_run]):
            self.engine._func_capture_metric_baseline()

        task_file = self.temp_dir / "team-management" / "tasks" / f"{self.TASK_NAME}.md"
        content = task_file.read_text(encoding="utf-8")
        self.assertIn("optimize.baseline_commit: deadbee", content)

    def test_metric_run_failure_propagates(self):
        with patch("protocol_engine.subprocess.run", return_value=self._make_completed(returncode=1, stdout="")):
            result = self.engine._func_capture_metric_baseline()
            self.assertFalse(result["success"])
            self.assertIn("baseline run failed", result["error"])

    def test_git_failure_records_dash_baseline_commit(self):
        metric_run = self._make_completed(stdout="1.0\n")
        git_fail = MagicMock()
        git_fail.returncode = 128
        git_fail.stdout = ""
        git_fail.stderr = "fatal"
        with patch("protocol_engine.subprocess.run", side_effect=[metric_run, git_fail]):
            result = self.engine._func_capture_metric_baseline()
            self.assertTrue(result["success"])
            self.assertEqual(result["baseline_commit"], "-")


class TestCheckCostEstimate(FuncTestBase):
    """_func_check_cost_estimate: bounded projection vs unbounded ack-required."""

    def test_bounded_returns_projection(self):
        self._write_optimize_state(
            baseline_wall_clock_s=2.0,
            runs_per_iteration=3,
            max_iterations=10,
            max_duration=None,
        )
        result = self.engine._func_check_cost_estimate()
        self.assertTrue(result["success"])
        self.assertFalse(result["unbounded"])
        self.assertEqual(result["projection"]["projected_wall_clock_s"], 60.0)

    def test_unbounded_without_ack_fails(self):
        self._write_optimize_state(
            baseline_wall_clock_s=2.0,
            runs_per_iteration=1,
            max_iterations=None,
            max_duration=None,
        )
        result = self.engine._func_check_cost_estimate()
        self.assertFalse(result["success"])
        self.assertTrue(result.get("unbounded"))
        self.assertIn("typed risk-checklist", result["error"])

    def test_unbounded_with_correct_ack_succeeds(self):
        self._write_optimize_state(
            baseline_wall_clock_s=2.0,
            runs_per_iteration=1,
            max_iterations=None,
            max_duration=None,
        )
        result = self.engine._func_check_cost_estimate(args={
            "unbounded_acknowledged": "i-accept-unbounded-cost",
        })
        self.assertTrue(result["success"])
        self.assertTrue(result.get("acknowledged"))

    def test_unbounded_with_wrong_ack_fails(self):
        self._write_optimize_state(
            baseline_wall_clock_s=2.0,
            runs_per_iteration=1,
            max_iterations=None,
            max_duration=None,
        )
        result = self.engine._func_check_cost_estimate(args={
            "unbounded_acknowledged": "yes please",
        })
        self.assertFalse(result["success"])


class TestCheckTermination(FuncTestBase):
    """_func_check_termination: 4 conditions, structured reason emit."""

    def _seed_tsv(self, *iter_value_status):
        """Helper: seed results.tsv with rows. Each arg is (iteration, value, status)."""
        header = "iteration\ttimestamp\tcommit_sha\tmetric_value\trun_count\taggregator\twall_clock_s\tstatus\thypothesis\n"
        rows = []
        for it, v, st in iter_value_status:
            rows.append(f"{it}\t2026-05-05T00:00\tabc\t{v}\t1\tmedian\t0.1\t{st}\trow{it}")
        self._tsv_path().write_text(header + "\n".join(rows) + "\n", encoding="utf-8")

    def test_max_iterations_terminates(self):
        # protocol state has loop_iteration=1; set max_iterations=1 in optimize-state
        self._write_optimize_state(max_iterations=1, metric_direction="min")
        self._seed_tsv((0, 10.0, "ok"), (1, 9.0, "ok"))
        result = self.engine._func_check_termination()
        self.assertTrue(result["success"])
        self.assertTrue(result["terminate"])
        self.assertEqual(result["reason"], "max_iterations")
        # Summary row appended
        tsv = self._tsv_path().read_text(encoding="utf-8")
        self.assertIn("TERMINATE max_iterations", tsv)

    def test_max_duration_terminates(self):
        # Override protocol experimentation_started_at to be 1 hour ago via current_task.json
        from datetime import datetime, timezone, timedelta
        long_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        ts = self._read_json(self.temp_dir / ".claude" / "state" / "current_task.json")
        ts["protocol"]["experimentation_started_at"] = long_ago
        self._write_json(self.temp_dir / ".claude" / "state" / "current_task.json", ts)

        self._write_optimize_state(max_duration=3600, metric_direction="min")  # 1h
        self._seed_tsv((0, 10.0, "ok"), (1, 9.0, "ok"))
        result = self.engine._func_check_termination()
        self.assertTrue(result["terminate"])
        self.assertEqual(result["reason"], "max_duration")

    def test_regression_halt_terminates(self):
        # 3 consecutive worsenings vs best
        self._write_optimize_state(regression_halt_n=3, metric_direction="min")
        # iter 0 best=5.0; iters 1,2,3 all 6.0 (worse since direction=min)
        self._seed_tsv(
            (0, 5.0, "ok"),
            (1, 6.0, "ok"),
            (2, 6.5, "ok"),
            (3, 7.0, "ok"),
        )
        result = self.engine._func_check_termination()
        self.assertTrue(result["terminate"])
        self.assertEqual(result["reason"], "regression_halt")

    def test_target_reached_terminates_min(self):
        self._write_optimize_state(target_metric=5.0, metric_direction="min")
        self._seed_tsv((0, 10.0, "ok"), (1, 4.5, "ok"))
        result = self.engine._func_check_termination()
        self.assertTrue(result["terminate"])
        self.assertEqual(result["reason"], "target_reached")

    def test_target_reached_terminates_max(self):
        self._write_optimize_state(target_metric=100.0, metric_direction="max")
        self._seed_tsv((0, 50.0, "ok"), (1, 105.0, "ok"))
        result = self.engine._func_check_termination()
        self.assertTrue(result["terminate"])
        self.assertEqual(result["reason"], "target_reached")

    def test_no_termination_when_progressing(self):
        self._write_optimize_state(
            max_iterations=100, regression_halt_n=5,
            target_metric=1.0, metric_direction="min",
        )
        self._seed_tsv((0, 10.0, "ok"), (1, 8.0, "ok"))
        result = self.engine._func_check_termination()
        self.assertTrue(result["success"])
        self.assertFalse(result["terminate"])

    def test_priority_max_iterations_wins_over_target(self):
        # Both max_iterations=1 (matched by loop_iteration=1) AND target met.
        # Max_iterations should fire first.
        self._write_optimize_state(
            max_iterations=1, target_metric=5.0, metric_direction="min",
        )
        self._seed_tsv((0, 10.0, "ok"), (1, 4.5, "ok"))
        result = self.engine._func_check_termination()
        self.assertTrue(result["terminate"])
        self.assertEqual(result["reason"], "max_iterations")


class TestUpdateBestCommit(FuncTestBase):
    """_func_update_best_commit: frontmatter writer for optimize.best_commit + best_metric."""

    def test_first_iteration_sets_best(self):
        self._write_optimize_state(metric_direction="min")
        result = self.engine._func_update_best_commit(args={
            "metric_value": 10.0,
            "commit_sha": "abc1234",
        })
        self.assertTrue(result["success"])
        self.assertTrue(result["updated"])
        self.assertEqual(result["new_best_commit"], "abc1234")
        # Frontmatter check
        task_file = self.temp_dir / "team-management" / "tasks" / f"{self.TASK_NAME}.md"
        content = task_file.read_text(encoding="utf-8")
        self.assertIn("optimize.best_commit: abc1234", content)
        self.assertIn("optimize.best_metric: 10.0", content)

    def test_min_improvement_updates(self):
        self._write_optimize_state(metric_direction="min")
        # Seed with a previous best of 10.0
        self.engine._set_optimize_field(self.TASK_NAME, "best_metric", "10.0")
        self.engine._set_optimize_field(self.TASK_NAME, "best_commit", "old1111")

        result = self.engine._func_update_best_commit(args={
            "metric_value": 5.5,  # better (lower)
            "commit_sha": "new5555",
        })
        self.assertTrue(result["updated"])

        task_file = self.temp_dir / "team-management" / "tasks" / f"{self.TASK_NAME}.md"
        content = task_file.read_text(encoding="utf-8")
        self.assertIn("optimize.best_commit: new5555", content)
        self.assertIn("optimize.best_metric: 5.5", content)
        self.assertNotIn("old1111", content)

    def test_min_worse_skips(self):
        self._write_optimize_state(metric_direction="min")
        self.engine._set_optimize_field(self.TASK_NAME, "best_metric", "5.0")
        self.engine._set_optimize_field(self.TASK_NAME, "best_commit", "good")

        result = self.engine._func_update_best_commit(args={
            "metric_value": 8.0,  # worse
            "commit_sha": "bad",
        })
        self.assertTrue(result["success"])
        self.assertFalse(result["updated"])
        # Frontmatter unchanged
        task_file = self.temp_dir / "team-management" / "tasks" / f"{self.TASK_NAME}.md"
        content = task_file.read_text(encoding="utf-8")
        self.assertIn("optimize.best_commit: good", content)
        self.assertNotIn("bad", content)

    def test_max_improvement_updates(self):
        self._write_optimize_state(metric_direction="max")
        self.engine._set_optimize_field(self.TASK_NAME, "best_metric", "50.0")
        self.engine._set_optimize_field(self.TASK_NAME, "best_commit", "old")

        result = self.engine._func_update_best_commit(args={
            "metric_value": 75.0,  # better (higher)
            "commit_sha": "new",
        })
        self.assertTrue(result["updated"])

    def test_nan_metric_skips(self):
        result = self.engine._func_update_best_commit(args={
            "metric_value": float("nan"),
            "commit_sha": "abc",
        })
        self.assertTrue(result["success"])
        self.assertFalse(result["updated"])

    def test_falls_back_to_last_tsv_row_and_git_head(self):
        """When called as a post_func without explicit args, update_best_commit
        reads metric_value from the last TSV row and commit_sha from
        `git rev-parse --short HEAD` (Codex round-3 critical: previously
        required explicit args that the engine never auto-injected)."""
        from shared_state import write_optimize_state
        write_optimize_state({"metric_direction": "min"})
        # Seed TSV with one ok row
        header = "iteration\ttimestamp\tcommit_sha\tmetric_value\trun_count\taggregator\twall_clock_s\tstatus\thypothesis\n"
        self._tsv_path().write_text(
            header + "1\t2026-05-05T00:00\tabc1234\t7.5\t1\tmedian\t0.1\tok\thyp\n",
            encoding="utf-8",
        )

        # Mock git rev-parse to produce a known SHA
        head_proc = MagicMock(returncode=0, stdout="head1234\n", stderr="")
        with patch("protocol_engine.subprocess.run", return_value=head_proc):
            result = self.engine._func_update_best_commit()  # NO args

        self.assertTrue(result["success"])
        self.assertTrue(result["updated"])
        self.assertEqual(result["new_best_metric"], 7.5)
        self.assertEqual(result["new_best_commit"], "head1234")

    def test_explicit_args_override_fallback(self):
        from shared_state import write_optimize_state
        write_optimize_state({"metric_direction": "min"})
        # Seed TSV with a different value
        header = "iteration\ttimestamp\tcommit_sha\tmetric_value\trun_count\taggregator\twall_clock_s\tstatus\thypothesis\n"
        self._tsv_path().write_text(
            header + "1\t2026-05-05T00:00\tabc1234\t7.5\t1\tmedian\t0.1\tok\thyp\n",
            encoding="utf-8",
        )
        # Explicit args take precedence
        result = self.engine._func_update_best_commit(args={
            "metric_value": 3.0,
            "commit_sha": "explicit",
        })
        self.assertTrue(result["updated"])
        self.assertEqual(result["new_best_metric"], 3.0)
        self.assertEqual(result["new_best_commit"], "explicit")

    def test_no_args_no_tsv_no_git_fails(self):
        # No TSV, no git → both fallbacks fail with clear error
        # First, no TSV file exists; mock git rev-parse to fail
        git_fail = MagicMock(returncode=128, stdout="", stderr="not a repo")
        with patch("protocol_engine.subprocess.run", return_value=git_fail):
            result = self.engine._func_update_best_commit()
        self.assertFalse(result["success"])
        self.assertIn("metric_value", result["error"])


class TestPolicyComplianceAudit(FuncTestBase):
    """_func_policy_compliance_audit: best-effort heuristic git scan."""

    def setUp(self):
        super().setUp()
        # Seed task frontmatter with baseline_commit and best_metric
        task_file = self.temp_dir / "team-management" / "tasks" / f"{self.TASK_NAME}.md"
        content = task_file.read_text(encoding="utf-8")
        new_content = content.replace(
            "created: 2026-05-05\n",
            "created: 2026-05-05\noptimize.baseline_commit: base1234\noptimize.best_metric: 5.5\n",
        )
        task_file.write_text(new_content, encoding="utf-8")

    def _git_log_response(self, body: str):
        m = MagicMock()
        m.returncode = 0
        m.stdout = body
        m.stderr = ""
        return m

    def test_no_baseline_skips(self):
        # Remove the baseline line
        task_file = self.temp_dir / "team-management" / "tasks" / f"{self.TASK_NAME}.md"
        content = task_file.read_text(encoding="utf-8").replace(
            "optimize.baseline_commit: base1234\n", ""
        )
        task_file.write_text(content, encoding="utf-8")

        result = self.engine._func_policy_compliance_audit()
        self.assertTrue(result["success"])
        self.assertIn("nothing to audit", result.get("skipped", ""))

    def test_frozen_path_modified_flagged(self):
        self._write_optimize_state(frozen_paths=["src/sacred.py"])
        # git log --name-only --pretty=format:%H base..HEAD
        log_body = (
            "abcdef1234567890\n"
            "src/sacred.py\n"
            "other/file.py\n"
            "\n"
            "deadbeef123456\n"
            "another.py\n"
        )
        diff_body = ""
        with patch("protocol_engine.subprocess.run", side_effect=[
            self._git_log_response(log_body),
            self._git_log_response(diff_body),
        ]):
            result = self.engine._func_policy_compliance_audit()
            self.assertTrue(result["success"])
            kinds = [f["kind"] for f in result["metric_gaming_flags"]]
            self.assertIn("frozen_path_modified", kinds)

    def test_results_tsv_edit_flagged(self):
        self._write_optimize_state(frozen_paths=[])
        log_body = (
            "abcdef1234567890\n"
            "team-management/tasks/o-test-funcs/results.tsv\n"
            "\n"
        )
        with patch("protocol_engine.subprocess.run", side_effect=[
            self._git_log_response(log_body),
            self._git_log_response(""),
        ]):
            result = self.engine._func_policy_compliance_audit()
            kinds = [f["kind"] for f in result["metric_gaming_flags"]]
            self.assertIn("results_tsv_edited", kinds)

    def test_metric_constant_hardcoded_flagged(self):
        self._write_optimize_state(frozen_paths=[])
        log_body = "abcdef1234567890\nsrc/foo.py\n\n"
        diff_body = (
            "commit abcdef\n"
            "diff --git a/src/foo.py b/src/foo.py\n"
            "+++ b/src/foo.py\n"
            "+    return 5.5  # metric constant\n"
        )
        with patch("protocol_engine.subprocess.run", side_effect=[
            self._git_log_response(log_body),
            self._git_log_response(diff_body),
        ]):
            result = self.engine._func_policy_compliance_audit()
            kinds = [f["kind"] for f in result["metric_gaming_flags"]]
            self.assertIn("metric_constant_hardcoded", kinds)

    def test_suffix_path_not_falsely_flagged(self):
        """R2-O4: vendor/src/sacred.py must NOT match frozen src/sacred.py —
        component-boundary matching, not suffix matching."""
        self._write_optimize_state(frozen_paths=["src/sacred.py"])
        log_body = (
            "abcdef1234567890abcdef1234567890abcdef12\n"
            "vendor/src/sacred.py\n"
            "\n"
        )
        with patch("protocol_engine.subprocess.run", side_effect=[
            self._git_log_response(log_body),
            self._git_log_response(""),
        ]):
            result = self.engine._func_policy_compliance_audit()
            kinds = [f["kind"] for f in result["metric_gaming_flags"]]
            self.assertNotIn("frozen_path_modified", kinds)

    def test_frozen_directory_matches_contained_file(self):
        """R2-O4: a frozen directory entry (src/) must flag files underneath
        it (src/sub/file.py) — the old suffix match silently missed these."""
        self._write_optimize_state(frozen_paths=["src/"])
        log_body = (
            "abcdef1234567890abcdef1234567890abcdef12\n"
            "src/sub/file.py\n"
            "\n"
        )
        with patch("protocol_engine.subprocess.run", side_effect=[
            self._git_log_response(log_body),
            self._git_log_response(""),
        ]):
            result = self.engine._func_policy_compliance_audit()
            kinds = [f["kind"] for f in result["metric_gaming_flags"]]
            self.assertIn("frozen_path_modified", kinds)

    def test_clean_history_no_flags(self):
        self._write_optimize_state(frozen_paths=["src/sacred.py"])
        log_body = "abcdef\nsrc/normal.py\n\n"
        diff_body = "diff --git a/src/normal.py b/src/normal.py\n+    pass\n"
        with patch("protocol_engine.subprocess.run", side_effect=[
            self._git_log_response(log_body),
            self._git_log_response(diff_body),
        ]):
            result = self.engine._func_policy_compliance_audit()
            self.assertTrue(result["success"])
            self.assertEqual(result["metric_gaming_flags"], [])


class TestBatchCheckpoint(FuncTestBase):
    """_func_batch_checkpoint: discussion-mode pause + approve_next_batch gate."""

    def setUp(self):
        super().setUp()
        self._write_optimize_state(batch_size=2, metric_direction="min")
        # Seed TSV with some rows
        header = "iteration\ttimestamp\tcommit_sha\tmetric_value\trun_count\taggregator\twall_clock_s\tstatus\thypothesis\n"
        rows = "\n".join([
            "0\t2026-05-05T00:00\tbase\t10.0\t1\tmedian\t0.1\tok\tbaseline",
            "1\t2026-05-05T00:01\tabc\t8.5\t1\tmedian\t0.1\tok\thypothesis 1",
            "2\t2026-05-05T00:02\tdef\t9.0\t1\tmedian\t0.1\tok\thypothesis 2",
        ])
        self._tsv_path().write_text(header + rows + "\n", encoding="utf-8")

    def test_blocks_advance_without_approval(self):
        result = self.engine._func_batch_checkpoint()
        self.assertFalse(result["success"])
        self.assertTrue(result.get("awaiting_approval"))
        # Summary in error message
        self.assertIn("Batch checkpoint", result["error"])
        self.assertIn("approve_next_batch", result["error"])
        self.assertIn("Best so far", result["error"])
        # batch_size respected
        self.assertEqual(result["batch_size"], 2)
        self.assertEqual(len(result["last_batch"]), 2)

    def test_switches_daic_to_discussion(self):
        from shared_state import check_daic_mode_raw
        # Start in implementation mode
        self._write_json(self.temp_dir / ".claude" / "state" / "daic-mode.json", {"mode": "implementation"})
        self.engine._func_batch_checkpoint()
        # After checkpoint, mode should be discussion
        self.assertEqual(check_daic_mode_raw(), "discussion")

    def test_approves_next_batch(self):
        result = self.engine._func_batch_checkpoint(args={"approve_next_batch": True})
        self.assertTrue(result["success"])
        self.assertTrue(result.get("approved"))


class TestWriteOptimizeSetup(FuncTestBase):
    """_func_write_optimize_setup: persist user-provided setup args to optimize-state.json.

    Closes the T2 gap where the setup step has no func to write user settings.
    The file is in PROTECTED_PATHS, so the setup step cannot edit it directly.
    """

    def _state_path(self) -> Path:
        return self.temp_dir / ".claude" / "state" / "optimize-state.json"

    def test_happy_path_persists_state_with_defaults(self):
        args = {
            "metric_command": "python3 metric.py",
            "metric_parser": r"^([0-9.]+)$",
            "metric_direction": "min",
            "metric_monotonic": True,
            "max_iterations": 10,
            "runs_per_iteration": 3,
            "aggregator": "median",
            "batch_size": 2,
            "frozen_paths": ["src/frozen.py"],
        }
        result = self.engine._func_write_optimize_setup(args)
        self.assertTrue(result["success"], result)
        state = self._read_json(self._state_path())
        # Explicit args persisted
        self.assertEqual(state["metric_command"], "python3 metric.py")
        self.assertEqual(state["metric_parser"], r"^([0-9.]+)$")
        self.assertEqual(state["metric_direction"], "min")
        self.assertEqual(state["metric_monotonic"], True)
        self.assertEqual(state["max_iterations"], 10)
        self.assertEqual(state["runs_per_iteration"], 3)
        self.assertEqual(state["aggregator"], "median")
        self.assertEqual(state["batch_size"], 2)
        self.assertEqual(state["frozen_paths"], ["src/frozen.py"])
        # Defaults filled
        self.assertEqual(state["max_duration"], 28800.0)  # "8h" normalised to seconds
        self.assertEqual(state["regression_halt_n"], 5)
        self.assertEqual(state["target_metric"], None)
        self.assertEqual(state["stability_threshold_pct"], 5.0)
        self.assertEqual(state["env_pass"], [])

    def test_missing_required_arg_fails(self):
        # Missing metric_parser, metric_direction, metric_monotonic
        args = {"metric_command": "python3 metric.py"}
        result = self.engine._func_write_optimize_setup(args)
        self.assertFalse(result["success"])
        self.assertIn("metric_parser", result["error"])
        self.assertIn("metric_direction", result["error"])
        self.assertIn("metric_monotonic", result["error"])
        self.assertFalse(self._state_path().exists())

    def test_invalid_direction_fails(self):
        args = {
            "metric_command": "python3 m.py", "metric_parser": r"^([0-9.]+)$",
            "metric_direction": "sideways", "metric_monotonic": True,
        }
        result = self.engine._func_write_optimize_setup(args)
        self.assertFalse(result["success"])
        self.assertIn("metric_direction", result["error"])
        self.assertIn("sideways", result["error"])

    def test_invalid_aggregator_fails(self):
        args = {
            "metric_command": "python3 m.py", "metric_parser": r"^([0-9.]+)$",
            "metric_direction": "min", "metric_monotonic": True,
            "aggregator": "geomean",
        }
        result = self.engine._func_write_optimize_setup(args)
        self.assertFalse(result["success"])
        self.assertIn("aggregator", result["error"])
        self.assertIn("geomean", result["error"])

    def test_typo_in_optimize_key_surfaces_did_you_mean(self):
        # `frozen_path` (singular) is a single-char-ish typo of `frozen_paths`
        # — without typo detection, the user silently loses safety. With it,
        # the result includes a suspicious_keys hint.
        args = {
            "metric_command": "python3 m.py", "metric_parser": r"^([0-9.]+)$",
            "metric_direction": "min", "metric_monotonic": True,
            "frozen_path": ["src/sensitive.py"],  # TYPO — singular
        }
        result = self.engine._func_write_optimize_setup(args)
        self.assertTrue(result["success"], result)  # Setup still succeeds
        self.assertIn("suspicious_keys", result)
        suspicious = result["suspicious_keys"]
        self.assertEqual(len(suspicious), 1)
        self.assertEqual(suspicious[0]["got"], "frozen_path")
        self.assertEqual(suspicious[0]["did_you_mean"], "frozen_paths")
        # And the actual frozen_paths in state should be empty (the typo went nowhere)
        state = self._read_json(self._state_path())
        self.assertEqual(state["frozen_paths"], [])

    def test_metric_monotonic_false_rejected(self):
        # v1 contract supports only monotonic metrics (defence-in-depth
        # against an LLM that ignores optimize-setup.md §1.3).
        args = {
            "metric_command": "python3 m.py", "metric_parser": r"^([0-9.]+)$",
            "metric_direction": "min", "metric_monotonic": False,
        }
        result = self.engine._func_write_optimize_setup(args)
        self.assertFalse(result["success"])
        self.assertIn("metric_monotonic", result["error"])
        self.assertIn("not supported", result["error"])
        self.assertFalse(self._state_path().exists())

    def test_max_duration_shorthand_normalised_to_seconds(self):
        for shorthand, expected_s in [
            ("8h", 28800.0),
            ("30m", 1800.0),
            ("90s", 90.0),
            ("3600", 3600.0),
            (3600, 3600.0),
            (3600.5, 3600.5),
            (None, None),
        ]:
            with self.subTest(shorthand=shorthand):
                args = {
                    "metric_command": "python3 m.py", "metric_parser": r"^([0-9.]+)$",
                    "metric_direction": "min", "metric_monotonic": True,
                    "max_duration": shorthand,
                }
                self._state_path().unlink(missing_ok=True)
                result = self.engine._func_write_optimize_setup(args)
                self.assertTrue(result["success"], f"{shorthand}: {result}")
                state = self._read_json(self._state_path())
                self.assertEqual(state["max_duration"], expected_s)

    def test_stability_threshold_zero_preserved(self):
        # stability_threshold_pct=0 means "strictly deterministic" — two
        # validator runs must produce identical values. The `or 5.0`
        # falsy-fallback would silently rewrite this to 5.0; None-only
        # fallback preserves the explicit 0.
        args = {
            "metric_command": "python3 m.py", "metric_parser": r"^([0-9.]+)$",
            "metric_direction": "min", "metric_monotonic": True,
            "stability_threshold_pct": 0,
        }
        result = self.engine._func_write_optimize_setup(args)
        self.assertTrue(result["success"], result)
        state = self._read_json(self._state_path())
        self.assertEqual(state["stability_threshold_pct"], 0.0)

    def test_termination_caps_strict_numeric_coercion(self):
        # max_iterations / regression_halt_n / target_metric must be coerced
        # strictly. A non-numeric value like "fifty" or "unbounded" must NOT
        # silently bypass safety caps via check_termination's silent
        # except: pass.
        for field, bad_value in [
            ("max_iterations", "fifty"),
            ("max_iterations", "unbounded"),
            ("regression_halt_n", "five"),
            ("target_metric", "best"),
        ]:
            with self.subTest(field=field, bad=bad_value):
                args = {
                    "metric_command": "python3 m.py", "metric_parser": r"^([0-9.]+)$",
                    "metric_direction": "min", "metric_monotonic": True,
                    field: bad_value,
                }
                self._state_path().unlink(missing_ok=True)
                result = self.engine._func_write_optimize_setup(args)
                self.assertFalse(result["success"], f"{field}={bad_value!r} should be rejected")
                self.assertIn("numeric arg coercion failed", result["error"])
                self.assertFalse(self._state_path().exists())

    def test_termination_caps_null_passthrough(self):
        # max_iterations=null and target_metric=null and regression_halt_n=null
        # must persist as None (not coerced to 0).
        args = {
            "metric_command": "python3 m.py", "metric_parser": r"^([0-9.]+)$",
            "metric_direction": "min", "metric_monotonic": True,
            "max_iterations": None, "target_metric": None,
            "regression_halt_n": None, "max_duration": None,
        }
        result = self.engine._func_write_optimize_setup(args)
        self.assertTrue(result["success"], result)
        state = self._read_json(self._state_path())
        self.assertIsNone(state["max_iterations"])
        self.assertIsNone(state["target_metric"])
        self.assertIsNone(state["regression_halt_n"])
        self.assertIsNone(state["max_duration"])

    def test_max_duration_invalid_string_rejected(self):
        for bad in ["forever", "8d", "8 hours", "1.5x"]:
            with self.subTest(bad=bad):
                args = {
                    "metric_command": "python3 m.py", "metric_parser": r"^([0-9.]+)$",
                    "metric_direction": "min", "metric_monotonic": True,
                    "max_duration": bad,
                }
                self._state_path().unlink(missing_ok=True)
                result = self.engine._func_write_optimize_setup(args)
                self.assertFalse(result["success"], f"{bad} should be rejected")
                self.assertIn("max_duration", result["error"])
                self.assertFalse(self._state_path().exists())

    def test_engine_flow_args_do_not_trigger_typo_warning(self):
        # `task`, `branch`, `task_content`, `carry_changes` are engine-flow
        # keys that flow through every advance. They must NOT trigger
        # suspicious_keys warnings (they are not optimize-specific keys).
        args = {
            "metric_command": "python3 m.py", "metric_parser": r"^([0-9.]+)$",
            "metric_direction": "min", "metric_monotonic": True,
            "task": "o-test", "branch": "optimize/test", "task_content": "...",
            "carry_changes": True,
        }
        result = self.engine._func_write_optimize_setup(args)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["suspicious_keys"], [])


class TestBatchCheckpointModulo(FuncTestBase):
    """_func_batch_checkpoint modulo gate: only fire on batch boundaries.

    Closes the T2 gap where batch_size was cosmetic (every iteration blocked).
    With batch_size=N, checkpoints fire after iter N-1, 2N-1, 3N-1, etc.
    """

    def _set_loop_iter(self, iter_value: int):
        state = self._read_json(self.temp_dir / ".claude" / "state" / "current_task.json")
        state.setdefault("protocol", {})["loop_iteration"] = iter_value
        self._write_json(self.temp_dir / ".claude" / "state" / "current_task.json", state)

    def setUp(self):
        super().setUp()
        # Seed minimal TSV so summary builders can read it
        header = "iteration\ttimestamp\tcommit_sha\tmetric_value\trun_count\taggregator\twall_clock_s\tstatus\thypothesis\n"
        rows = "\n".join([
            "0\t2026-05-05T00:00\tbase\t10.0\t1\tmedian\t0.1\tok\tbaseline",
            "1\t2026-05-05T00:01\tabc\t8.5\t1\tmedian\t0.1\tok\thypothesis 1",
        ])
        self._tsv_path().write_text(header + rows + "\n", encoding="utf-8")

    def test_mid_batch_returns_success_no_block(self):
        # batch_size=3, just-completed iter 0 → (0+1)%3=1 → mid-batch → no-op
        self._write_optimize_state(batch_size=3, metric_direction="min")
        self._set_loop_iter(0)
        result = self.engine._func_batch_checkpoint()
        self.assertTrue(result["success"], result)
        self.assertFalse(result.get("awaiting_approval", False))

    def test_batch_boundary_blocks_without_approval(self):
        # batch_size=3, just-completed iter 2 → (2+1)%3=0 → boundary → blocks
        self._write_optimize_state(batch_size=3, metric_direction="min")
        self._set_loop_iter(2)
        result = self.engine._func_batch_checkpoint()
        self.assertFalse(result["success"])
        self.assertTrue(result.get("awaiting_approval"))
        self.assertIn("Batch checkpoint", result["error"])

    def test_batch_boundary_advances_with_approval(self):
        # Same boundary, but approve_next_batch=True → success
        self._write_optimize_state(batch_size=3, metric_direction="min")
        self._set_loop_iter(2)
        result = self.engine._func_batch_checkpoint(args={"approve_next_batch": True})
        self.assertTrue(result["success"])
        self.assertTrue(result.get("approved"))

    def test_batch_size_one_blocks_every_iteration(self):
        # batch_size=1 → (N+1)%1==0 always → every iteration is a boundary
        self._write_optimize_state(batch_size=1, metric_direction="min")
        for iter_value in (0, 1, 5):
            self._set_loop_iter(iter_value)
            result = self.engine._func_batch_checkpoint()
            self.assertFalse(result["success"], f"iter {iter_value} should block")

    def test_exit_loop_releases_gate_at_boundary(self):
        # exit_loop=True must release the batch_checkpoint gate so the
        # engine's downstream loop-exit logic can fire. Without this, the
        # documented escape hatch (`args={"exit_loop": True}`) only works
        # when paired with the unrelated approve_next_batch flag.
        self._write_optimize_state(batch_size=2, metric_direction="min")
        self._set_loop_iter(1)  # boundary
        result = self.engine._func_batch_checkpoint(args={"exit_loop": True})
        self.assertTrue(result["success"], result)
        self.assertTrue(result.get("exit_loop"))

    def test_terminate_at_boundary_skips_block(self):
        # When check_termination fires at the same iteration as a batch boundary,
        # batch_checkpoint must NOT block — the loop is over. The terminate
        # signal is detectable via the TSV summary row that check_termination
        # appends.
        self._write_optimize_state(batch_size=2, metric_direction="min")
        self._set_loop_iter(1)  # boundary: (1+1)%2 == 0
        # Append a summary row simulating check_termination's terminate path
        header = "iteration\ttimestamp\tcommit_sha\tmetric_value\trun_count\taggregator\twall_clock_s\tstatus\thypothesis\n"
        rows = "\n".join([
            "0\t2026-05-05T00:00\tbase\t10.0\t1\tmedian\t0.1\tok\tbaseline",
            "1\t2026-05-05T00:01\tabc\t8.5\t1\tmedian\t0.1\tok\thypothesis 1",
            "-1\t2026-05-05T00:02\t-\tNaN\t0\tmedian\t0\tsummary\tmax_iterations",
        ])
        self._tsv_path().write_text(header + rows + "\n", encoding="utf-8")
        result = self.engine._func_batch_checkpoint()
        self.assertTrue(result["success"], result)
        self.assertTrue(result.get("terminated_skip"))


class TestAllFuncsRegistered(FuncTestBase):
    """Sanity: all 11 optimize funcs are registered in _build_handlers (9 from T2 + validate_optimize_setup + write_optimize_setup from T4)."""

    def test_all_funcs_resolved(self):
        handlers = self.engine._build_handlers()
        for name in (
            "log_experiment_result", "run_metric", "validate_metric_script",
            "capture_metric_baseline", "check_cost_estimate", "check_termination",
            "update_best_commit", "policy_compliance_audit", "batch_checkpoint",
            "validate_optimize_setup", "write_optimize_setup",
        ):
            self.assertIn(name, handlers, f"{name} not registered in _build_handlers")


class TestExperimentationControlCallShortCircuit(FuncTestBase):
    """Regression: log_experiment_result, update_best_commit, and check_termination
    must short-circuit when called on a control-call advance (approve_next_batch
    or exit_loop alone). Without this, re-running post_funcs after a
    batch-boundary block would write a spurious duplicate row, re-process
    update_best_commit on the same iteration, and re-evaluate termination.
    """

    def test_log_experiment_result_skips_on_approve_next_batch(self):
        # Seed a TSV with one row to make the no-op verifiable
        self._write_optimize_state(metric_direction="min")
        header = "iteration\ttimestamp\tcommit_sha\tmetric_value\trun_count\taggregator\twall_clock_s\tstatus\thypothesis\n"
        self._tsv_path().write_text(header + "0\t2026-05-05T00:00\tabc\t10.0\t1\tmedian\t0.1\tok\th0\n", encoding="utf-8")
        before = self._tsv_path().read_text()
        # Control-call short-circuit must run BEFORE _func_run_metric — releasing
        # a checkpoint gate must not pay for an unnecessary metric subprocess.
        with patch.object(self.engine, "_func_run_metric") as mock_metric:
            result = self.engine._func_log_experiment_result(args={"approve_next_batch": True})
        self.assertTrue(result["success"], result)
        self.assertIn("control-call", result.get("skipped_reason", ""))
        self.assertEqual(mock_metric.call_count, 0)
        # TSV unchanged
        self.assertEqual(self._tsv_path().read_text(), before)

    def test_log_experiment_result_skips_on_exit_loop(self):
        self._write_optimize_state(metric_direction="min")
        header = "iteration\ttimestamp\tcommit_sha\tmetric_value\trun_count\taggregator\twall_clock_s\tstatus\thypothesis\n"
        self._tsv_path().write_text(header + "0\t2026-05-05T00:00\tabc\t10.0\t1\tmedian\t0.1\tok\th0\n", encoding="utf-8")
        before = self._tsv_path().read_text()
        with patch.object(self.engine, "_func_run_metric") as mock_metric:
            result = self.engine._func_log_experiment_result(args={"exit_loop": True})
        self.assertTrue(result["success"], result)
        self.assertEqual(mock_metric.call_count, 0)
        self.assertEqual(self._tsv_path().read_text(), before)

    def test_update_best_commit_skips_on_approve_next_batch(self):
        result = self.engine._func_update_best_commit(args={"approve_next_batch": True})
        self.assertTrue(result["success"], result)
        self.assertIn("control-call", result.get("skipped_reason", ""))

    def test_update_best_commit_skips_on_exit_loop(self):
        result = self.engine._func_update_best_commit(args={"exit_loop": True})
        self.assertTrue(result["success"], result)

    def test_check_termination_skips_on_approve_next_batch(self):
        # Configure max_iterations=1 with loop_iteration=5 — would normally
        # terminate. Control-call must skip.
        self._write_optimize_state(metric_direction="min", max_iterations=1)
        self._set_loop_iter(5)
        result = self.engine._func_check_termination(args={"approve_next_batch": True})
        self.assertTrue(result["success"], result)
        self.assertFalse(result.get("terminate"))
        self.assertIn("control-call", result.get("skipped_reason", ""))

    def test_check_termination_skips_on_exit_loop(self):
        self._write_optimize_state(metric_direction="min", max_iterations=1)
        self._set_loop_iter(5)
        result = self.engine._func_check_termination(args={"exit_loop": True})
        self.assertTrue(result["success"], result)
        self.assertFalse(result.get("terminate"))

    def _set_loop_iter(self, iter_value: int):
        state = self._read_json(self.temp_dir / ".claude" / "state" / "current_task.json")
        state.setdefault("protocol", {})["loop_iteration"] = iter_value
        self._write_json(self.temp_dir / ".claude" / "state" / "current_task.json", state)


class TestValidateOptimizeSetup(FuncTestBase):
    """_func_validate_optimize_setup: early-gate validation without disk write.

    Pairs with write_optimize_setup as the last post_func — this one runs
    first so a validation failure aborts before git_setup_branch /
    create_task_file / create_issue_if_enabled create durable side effects.
    """

    def _state_path(self) -> Path:
        return self.temp_dir / ".claude" / "state" / "optimize-state.json"

    def test_valid_args_succeed_without_writing(self):
        args = {
            "metric_command": "python3 m.py", "metric_parser": r"^([0-9.]+)$",
            "metric_direction": "min", "metric_monotonic": True,
            "max_iterations": 10, "batch_size": 2,
        }
        result = self.engine._func_validate_optimize_setup(args)
        self.assertTrue(result["success"], result)
        self.assertIn("state_preview", result)
        self.assertEqual(result["state_preview"]["max_iterations"], 10)
        # Critical: no disk write
        self.assertFalse(self._state_path().exists())

    def test_invalid_args_fail_without_writing(self):
        args = {
            "metric_command": "python3 m.py", "metric_parser": r"^([0-9.]+)$",
            "metric_direction": "sideways",  # invalid
            "metric_monotonic": True,
        }
        result = self.engine._func_validate_optimize_setup(args)
        self.assertFalse(result["success"])
        self.assertIn("metric_direction", result["error"])
        self.assertFalse(self._state_path().exists())

    def test_metric_monotonic_must_be_strict_bool(self):
        # `bool("false") == True` would defeat the v1 monotonic gate.
        # Strict isinstance(..., bool) check rejects non-bool truthy values.
        for bad_value in ["false", "False", "0", 0, 1, "true"]:
            with self.subTest(bad=bad_value):
                args = {
                    "metric_command": "python3 m.py", "metric_parser": r"^([0-9.]+)$",
                    "metric_direction": "min", "metric_monotonic": bad_value,
                }
                result = self.engine._func_validate_optimize_setup(args)
                self.assertFalse(result["success"], f"{bad_value!r} should be rejected")
                self.assertIn("metric_monotonic", result["error"])
                self.assertIn("boolean", result["error"])

    def test_frozen_paths_must_be_list_not_string(self):
        # `list("src/foo.py")` corrupts to ['s','r','c','/','f','o','o',...]
        # which would silently disable frozen-path enforcement for the real
        # path. Strict isinstance(..., list) check rejects scalar strings.
        args = {
            "metric_command": "python3 m.py", "metric_parser": r"^([0-9.]+)$",
            "metric_direction": "min", "metric_monotonic": True,
            "frozen_paths": "src/sensitive.py",  # scalar string, not list
        }
        result = self.engine._func_validate_optimize_setup(args)
        self.assertFalse(result["success"])
        self.assertIn("frozen_paths", result["error"])
        self.assertIn("list", result["error"])

    def test_env_pass_must_be_list_not_string(self):
        args = {
            "metric_command": "python3 m.py", "metric_parser": r"^([0-9.]+)$",
            "metric_direction": "min", "metric_monotonic": True,
            "env_pass": "MY_VAR",
        }
        result = self.engine._func_validate_optimize_setup(args)
        self.assertFalse(result["success"])
        self.assertIn("env_pass", result["error"])
        self.assertIn("list", result["error"])


# ============================================================================
# AI provider pre_func tests (h-ai-providers-foundation — Engine stream)
# ============================================================================

class _AIProviderPhaseTestMixin:
    """Mixin (not subclassing TestCase directly so it doesn't run alone).

    Concrete subclasses set PHASE_KEY and inherit from TestCase. Provides setUp
    that wires a temp project tree with a config.json, daic-mode.json, and a
    task file, then patches shared_state globals to the temp project root.
    """
    PHASE_KEY = None  # override in subclass

    TASK_NAME = "h-ai-providers-test"
    BRANCH = "feature/ai-providers-test"

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        (self.temp_dir / ".claude" / "state").mkdir(parents=True)
        (self.temp_dir / "team-management" / "tasks").mkdir(parents=True)
        (self.temp_dir / "team-management" / "protocol-configs" / "system" / "providers").mkdir(parents=True)
        (self.temp_dir / "team-management" / "protocol-configs" / "custom" / "providers").mkdir(parents=True)

        self._write_json(self.temp_dir / ".claude" / "state" / "current_task.json", {
            "task": self.TASK_NAME,
            "branch": self.BRANCH,
            "services": [],
            "updated": "2026-05-29",
        })
        self._write_json(self.temp_dir / ".claude" / "state" / "daic-mode.json", {"mode": "implementation"})

        task_file = self.temp_dir / "team-management" / "tasks" / f"{self.TASK_NAME}.md"
        task_file.write_text(
            "---\n"
            f"task: {self.TASK_NAME}\n"
            f"branch: {self.BRANCH}\n"
            "status: in-progress\n"
            "---\n\n# AI providers test task\n\nSome content.\n",
            encoding="utf-8",
        )

        import shared_state
        self._orig_pr = shared_state.PROJECT_ROOT
        self._orig_sd = shared_state.STATE_DIR
        self._orig_ts = shared_state.TASK_STATE_FILE
        self._orig_ds = shared_state.DAIC_STATE_FILE
        shared_state.PROJECT_ROOT = self.temp_dir
        shared_state.STATE_DIR = self.temp_dir / ".claude" / "state"
        shared_state.TASK_STATE_FILE = self.temp_dir / ".claude" / "state" / "current_task.json"
        shared_state.DAIC_STATE_FILE = self.temp_dir / ".claude" / "state" / "daic-mode.json"

        from protocol_engine import ProtocolEngine
        self.engine = ProtocolEngine(self.temp_dir)

    def tearDown(self):
        import shared_state
        shared_state.PROJECT_ROOT = self._orig_pr
        shared_state.STATE_DIR = self._orig_sd
        shared_state.TASK_STATE_FILE = self._orig_ts
        shared_state.DAIC_STATE_FILE = self._orig_ds
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_json(self, path: Path, data: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _write_config(self, ai_overrides=None, codex_enabled=True, agy_enabled=True,
                       enabled_providers=("codex", "agy")):
        """Write config.json. ai_overrides merges into ai_providers block."""
        from protocol_engine import _PHASE_REGISTRY
        entry = _PHASE_REGISTRY[self.PHASE_KEY]
        ai_block = {
            "enabled_providers": list(enabled_providers),
            entry["config_flag"]: True,
        }
        if ai_overrides:
            ai_block.update(ai_overrides)
        cfg = {
            "ai_providers": ai_block,
            "codex": {"enabled": codex_enabled},
            "agy": {"enabled": agy_enabled},
        }
        config_path = self.temp_dir / "team-management" / "config.json"
        config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    def _pre_func(self):
        """Invoke the phase's pre_func method on the engine."""
        from protocol_engine import _PHASE_REGISTRY
        entry = _PHASE_REGISTRY[self.PHASE_KEY]
        method = getattr(self.engine, f"_func_{entry['func_name']}")
        return method()

    def test_disabled_when_flag_false(self):
        from protocol_engine import _PHASE_REGISTRY
        flag = _PHASE_REGISTRY[self.PHASE_KEY]["config_flag"]
        self._write_config(ai_overrides={flag: False})
        result = self._pre_func()
        self.assertTrue(result["success"])
        self.assertEqual(result["providers"], [])
        self.assertNotIn("PARALLEL AI PROVIDERS", result["instructions"])

    def test_enabled_codex_only(self):
        self._write_config(enabled_providers=["codex"], agy_enabled=False)
        result = self._pre_func()
        self.assertEqual(result["providers"], ["codex"])
        self.assertIn("<codex-output>", result["instructions"])
        self.assertNotIn("<agy-output>", result["instructions"])
        self.assertIn("participating in", result["instructions"])

    def test_enabled_both_providers(self):
        self._write_config(enabled_providers=["codex", "agy"])
        result = self._pre_func()
        self.assertEqual(sorted(result["providers"]), ["agy", "codex"])
        self.assertIn("<codex-output>", result["instructions"])
        self.assertIn("<agy-output>", result["instructions"])
        self.assertIn("codex + agy participating in", result["instructions"])

    def test_stale_gemini_entry_ignored(self):
        """A leftover `"gemini"` in enabled_providers (pre-replacement config)
        must be silently ignored — no KeyError, no third provider, no gemini
        output delimiters in the instructions."""
        self._write_config(enabled_providers=["codex", "gemini", "agy"])
        result = self._pre_func()
        self.assertTrue(result["success"])
        self.assertEqual(sorted(result["providers"]), ["agy", "codex"])
        self.assertNotIn("<gemini-output>", result["instructions"])

    def test_missing_config(self):
        # No config.json present
        result = self._pre_func()
        self.assertTrue(result["success"])
        self.assertEqual(result["providers"], [])
        self.assertIn("No config file found", result["instructions"])

    def test_malformed_config_degrades_gracefully(self):
        """Code-review W4 regression: the dispatcher re-raise must stay NARROW
        (SandboxFlagError, the dedicated M3 subclass) — a broad
        `except ValueError: raise` would also propagate json.JSONDecodeError
        (a ValueError subclass), crashing the protocol step on a malformed
        config.json instead of degrading gracefully."""
        config_path = self.temp_dir / "team-management" / "config.json"
        config_path.write_text("{not valid json", encoding="utf-8")
        result = self._pre_func()
        self.assertTrue(result["success"])
        self.assertEqual(result["providers"], [])
        self.assertIn("Config read error", result["instructions"])

    def test_byte_malformed_config_degrades_gracefully(self):
        """Round-2 code-review warning: UnicodeDecodeError is ALSO a ValueError
        subclass (raised by json.load on non-UTF-8 bytes, e.g. a config.json
        hand-edited on Windows in cp1251). It must degrade gracefully — only
        the dedicated SandboxFlagError may propagate out of the dispatcher."""
        config_path = self.temp_dir / "team-management" / "config.json"
        config_path.write_bytes(b'{"developer_name": "\xcc\xe0\xea\xf1"}')
        result = self._pre_func()
        self.assertTrue(result["success"])
        self.assertEqual(result["providers"], [])
        self.assertIn("Config read error", result["instructions"])

    def test_output_validation_non_empty_assertion(self):
        """Pre_func must instruct main agent to treat empty wrapper output as
        non-blocking failure. This is the contract-level expression of R3-1."""
        self._write_config()
        result = self._pre_func()
        self.assertIn("empty", result["instructions"].lower())
        self.assertIn("non-blocking failure", result["instructions"])

    def test_credential_filter_redacts_plan_summary(self):
        """The task file content substituted into {plan_summary} must be filtered."""
        # Re-write task file with a credential-pattern line
        task_file = self.temp_dir / "team-management" / "tasks" / f"{self.TASK_NAME}.md"
        task_file.write_text(
            "---\nstatus: in-progress\n---\n\n"
            "AWS_SECRET_ACCESS_KEY=AKIAIOSFODNN7EXAMPLE\n"
            "Normal line of plan content.\n",
            encoding="utf-8",
        )
        self._write_config()
        result = self._pre_func()
        # The substituted {plan_summary} would normally carry the AWS line,
        # but the filter must have redacted it before injection.
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", result["instructions"])
        self.assertIn("[REDACTED:", result["instructions"])

    def test_sandbox_flag_assertion_raises_on_drift(self):
        """If a custom template omits the sandbox flag, the assertion fires."""
        from protocol_engine import _PHASE_REGISTRY
        entry = _PHASE_REGISTRY[self.PHASE_KEY]
        # Write a custom codex template missing `-s read-only`
        custom_path = (self.temp_dir / "team-management" / "protocol-configs"
                       / "custom" / "providers" / f"codex-{entry['template_subpath']}.md")
        custom_path.write_text(
            "Bogus prompt without sandbox flag (test of assertion). "
            "agy uses --sandbox as required.",
            encoding="utf-8",
        )
        self._write_config(enabled_providers=["codex"], agy_enabled=False)
        # ValueError, not AssertionError: the check survives `python -O` (M3,
        # m-harden-ai-provider-layer) — the old assert encoded the bug.
        with self.assertRaises(ValueError):
            self._pre_func()

    def test_agy_sandbox_assertion_raises_on_drift(self):
        """If a custom agy template omits `--sandbox`, the assertion fires."""
        from protocol_engine import _PHASE_REGISTRY
        entry = _PHASE_REGISTRY[self.PHASE_KEY]
        custom_path = (self.temp_dir / "team-management" / "protocol-configs"
                       / "custom" / "providers" / f"agy-{entry['template_subpath']}.md")
        custom_path.write_text(
            "Bogus agy prompt without the sandbox flag (test of assertion).",
            encoding="utf-8",
        )
        self._write_config(enabled_providers=["agy"], codex_enabled=False)
        # ValueError, not AssertionError — see the codex drift test above (M3).
        with self.assertRaises(ValueError):
            self._pre_func()

    def test_agy_inline_default_passes_sandbox_assertion(self):
        """With NO agy template on disk anywhere, the inline default template
        must carry the literal `--sandbox` so the assertion does not fire and
        the pre_func succeeds (cold-start / missing-template degradation)."""
        # The temp project root has no template dirs at all — the 3-tier disk
        # lookup misses and the inline default is used.
        self._write_config(enabled_providers=["agy"], codex_enabled=False)
        result = self._pre_func()
        self.assertTrue(result["success"])
        self.assertEqual(result["providers"], ["agy"])
        self.assertIn("--sandbox", result["instructions"])


class CredentialFilterTest(TestCase):
    """Tests for _filter_credentials covering the 17 named patterns + the
    JSON/YAML-tolerant relaxation of `token` / `password` separators.

    The whole-line redaction semantic is the contract — if any pattern matches
    anywhere on the line, the entire line is replaced with `[REDACTED:<reason>]`.
    """

    def setUp(self):
        import shared_state
        from protocol_engine import ProtocolEngine
        self.engine = ProtocolEngine(Path("."))

    def _redacts(self, text, expected_reason=None):
        out = self.engine._filter_credentials(text)
        self.assertIn("[REDACTED:", out)
        if expected_reason:
            self.assertIn(f"[REDACTED:{expected_reason}]", out)
        return out

    def test_shell_token_equals(self):
        self._redacts("token=ghp_AAAAAAAAAAAAAAAAA\n", "token")

    def test_yaml_token_colon(self):
        """Codex code-review Warning #2 regression: YAML-style separator."""
        self._redacts("token: ghp_AAAAAAAAAAAAAAAAA\n", "token")

    def test_yaml_token_with_space(self):
        self._redacts("token : ghp_AAAAAAAAAAAAAAAAA\n", "token")

    def test_json_token_colon_quoted(self):
        self._redacts('  "token": "ghp_AAAAAAAAAAAAAAAAA",\n', "token")

    def test_shell_password_equals(self):
        self._redacts("password=hunter2\n", "password")

    def test_yaml_password_colon(self):
        self._redacts("password: hunter2\n", "password")

    def test_dotenv_pattern(self):
        self._redacts("Source: see project.env file\n", "dotenv")

    def test_bearer_token(self):
        self._redacts("Authorization: Bearer abcdef1234567890abcdef1234567890\n", "bearer-token")

    def test_private_key_header(self):
        self._redacts("-----BEGIN RSA PRIVATE KEY-----\n", "private-key")

    def test_postgres_url(self):
        self._redacts("DATABASE_URL=postgres://user:pw@host:5432/db\n", "postgres-url")

    def test_aws_access_key(self):
        self._redacts("AWS_ACCESS_KEY_ID = AKIA12345\n", "aws-credential")

    def test_clean_line_not_redacted(self):
        out = self.engine._filter_credentials("This is a normal line.\n")
        self.assertNotIn("[REDACTED:", out)

    def test_empty_input(self):
        self.assertEqual(self.engine._filter_credentials(""), "")

    def test_preserves_line_endings(self):
        out = self.engine._filter_credentials("token=secret\r\nfoo\n")
        self.assertIn("\r\n", out)
        self.assertIn("foo\n", out)

    # --- M4: prose must pass through (anchored patterns) -------------------

    def _passes(self, text):
        out = self.engine._filter_credentials(text)
        self.assertNotIn("[REDACTED:", out)
        self.assertEqual(out, text)

    def test_audit_inscope_line_passes(self):
        """Live regression from r-framework-audit: this exact In-Scope line was
        redacted to [REDACTED:credentials], degrading provider context."""
        self._passes("security-relevant logic (credential filter, sandbox assertions)\n")

    def test_prose_secret_passes(self):
        self._passes("the secret sauce of this design\n")

    def test_prose_credentials_passes(self):
        self._passes("The credential filter is simultaneously too broad\n")

    # --- M4: assignment context still redacted -----------------------------

    def test_credentials_assignment_redacted(self):
        self._redacts("credentials = service-account.json\n", "credentials")

    def test_client_secret_redacted(self):
        self._redacts("client_secret=abc123\n", "secret")

    def test_secret_yaml_redacted(self):
        self._redacts("secret: hunter2\n", "secret")

    # --- M4: compound *_token names ----------------------------------------

    def test_access_token_colon(self):
        self._redacts("access_token: abc123\n", "token")

    def test_auth_token_equals(self):
        self._redacts("auth_token=abc123\n", "token")

    def test_refresh_token_colon(self):
        self._redacts("refresh_token: abc123\n", "token")

    # --- R2-3: secret value formats -----------------------------------------

    def test_github_pat_value(self):
        self._redacts("see " + "ghp_" + "A1b2C3d4" * 5 + "\n", "github-pat")

    def test_github_fine_grained_pat_value(self):
        self._redacts("github_pat_11AAAAAAA0aaaaaaaaaaaa\n", "github-pat")

    def test_slack_token_value(self):
        self._redacts("hook uses xoxb-123456789012-abcdefghijkl\n", "slack-token")

    def test_aws_access_key_id_value(self):
        """Value-format AKIA pattern: the old aws-credential pattern only matched
        the variable NAME (aws_access_/aws_secret_), never a bare key value."""
        self._redacts("key is AKIAIOSFODNN7EXAMPLE\n", "aws-access-key-id")

    def test_aws_temporary_key_id_value(self):
        """ASIA prefix = temporary (STS) access key id — same leak class as AKIA."""
        self._redacts("key is ASIAIOSFODNN7EXAMPLE\n", "aws-access-key-id")

    def test_jwt_value(self):
        self._redacts(
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.SflKxwRJSMeKKF2QT4\n", "jwt")

    # --- R2-3: multi-line PEM block fully scrubbed ---------------------------

    def test_pem_block_fully_scrubbed(self):
        text = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n"
            "BKcwggSjAgEAAoIBAQDXk9q1zXyW8a2v\n"
            "Qm9w0BAQEFAASCBKcwggSjAgEAAoIBAQ\n"
            "-----END RSA PRIVATE KEY-----\n"
            "Normal line after the key block.\n"
        )
        out = self.engine._filter_credentials(text)
        self.assertEqual(out.count("[REDACTED:private-key]"), 5, out)
        self.assertIn("Normal line after the key block.\n", out)
        self.assertNotIn("MIIEvQIBAD", out)
        self.assertNotIn("BKcwggSj", out)

    def test_pem_dual_match_header_still_scrubs_body(self):
        """Code-review W1 regression: when the BEGIN line ALSO matches an earlier
        pattern (first-match-wins picks its reason), the PEM state machine must
        still arm — otherwise the base64 body leaks."""
        text = (
            'credentials = "-----BEGIN RSA PRIVATE KEY-----\n'
            "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n"
            '-----END RSA PRIVATE KEY-----"\n'
            "after\n"
        )
        out = self.engine._filter_credentials(text)
        self.assertNotIn("MIIEvQIBAD", out)
        self.assertIn("[REDACTED:credentials]", out)  # header keeps first-match label
        self.assertEqual(out.count("[REDACTED:private-key]"), 2, out)
        self.assertIn("after\n", out)

    def test_pem_state_does_not_leak_past_end(self):
        """Filter state machine must reset after END so later text is judged
        by the normal per-line patterns again."""
        text = (
            "-----BEGIN OPENSSH PRIVATE KEY-----\n"
            "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUA\n"
            "-----END OPENSSH PRIVATE KEY-----\n"
            "plain prose\n"
            "password: hunter2\n"
        )
        out = self.engine._filter_credentials(text)
        self.assertIn("plain prose\n", out)
        self.assertIn("[REDACTED:password]", out)
        self.assertEqual(out.count("[REDACTED:private-key]"), 3, out)


class SandboxFlagEnforcementTest(TestCase):
    """M3: sandbox-flag trust-boundary check must survive `python -O`.

    Bare `assert` statements are stripped by the optimizer; the check now lives
    in module-level `_ensure_sandbox_flags` which raises ValueError explicitly.
    """

    def setUp(self):
        from ai_providers import _ensure_sandbox_flags
        self.check = _ensure_sandbox_flags

    def test_codex_exec_missing_flag_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self.check(
                enabled=["codex"], subcommand="exec",
                codex_task="codex exec without the flag",
                agy_task="agy --sandbox -p x",
                func_name="resolve_ai_providers_for_investigation",
                phase="task investigation",
            )
        self.assertIn("-s read-only", str(ctx.exception))

    def test_agy_missing_sandbox_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self.check(
                enabled=["agy"], subcommand="exec",
                codex_task="codex exec -s read-only -p x",
                agy_task="agy without the flag",
                func_name="resolve_ai_providers_for_investigation",
                phase="task investigation",
            )
        self.assertIn("--sandbox", str(ctx.exception))

    def test_valid_flags_pass(self):
        self.check(
            enabled=["codex", "agy"], subcommand="exec",
            codex_task="codex exec -s read-only -p x",
            agy_task="agy --sandbox -p x",
            func_name="f", phase="p",
        )

    def test_codex_review_subcommand_skips_codex_check(self):
        """`codex review` has its own sandbox — no `-s read-only` required."""
        self.check(
            enabled=["codex"], subcommand="review",
            codex_task="codex review --uncommitted",
            agy_task="agy --sandbox -p x",
            func_name="f", phase="p",
        )

    def test_check_fires_under_python_dash_O(self):
        """The whole point of M3: under `python -O` a bare assert vanishes.
        Run the check in a -O subprocess and require the ValueError."""
        code = (
            "import sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from ai_providers import _ensure_sandbox_flags\n"
            "try:\n"
            "    _ensure_sandbox_flags(enabled=['agy'], subcommand='exec',\n"
            "                          codex_task='', agy_task='no flag',\n"
            "                          func_name='f', phase='p')\n"
            "except ValueError:\n"
            "    sys.exit(42)\n"
            "sys.exit(0)\n"
        )
        result = subprocess.run(
            [sys.executable, "-O", "-c", code, str(HOOKS_DIR)],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 42, result.stderr)


class BrainstormAIProviderTest(_AIProviderPhaseTestMixin, TestCase):
    PHASE_KEY = "brainstorm"


class InvestigationAIProviderTest(_AIProviderPhaseTestMixin, TestCase):
    PHASE_KEY = "investigation"


class ImplementationAIProviderTest(_AIProviderPhaseTestMixin, TestCase):
    PHASE_KEY = "implementation"


class RefactoringPlanningAIProviderTest(_AIProviderPhaseTestMixin, TestCase):
    PHASE_KEY = "refactoring_planning"


class ResearchExplorationAIProviderTest(_AIProviderPhaseTestMixin, TestCase):
    """Existing exploration pre_func — verifies the renamed config flag works."""
    PHASE_KEY = "research_exploration"


class TestCreateMergeRequest(FuncTestBase):
    """_func_create_merge_request honest reporting + non-fatal contract
    (h-fix-mcp-token-and-claude-gitignore)."""

    def _write_config(self, provider):
        self._write_json(self.temp_dir / "team-management" / "config.json",
                         {"issue_tracking": {"provider": provider},
                          provider: {"enabled": True}})

    def test_github_none_sync_reports_skipped_not_unimplemented(self):
        self._write_config("github")
        with patch("github_utils.get_github_sync", return_value=None):
            r = self.engine._func_create_merge_request({})
        self.assertTrue(r["success"])  # non-fatal: chain must not abort
        self.assertEqual(r["action"], "skipped")
        self.assertEqual(r["provider"], "github")
        self.assertNotIn("not implemented", json.dumps(r).lower())
        self.assertIn("token", r["reason"].lower())

    def test_github_failed_pr_reports_failed_action(self):
        self._write_config("github")
        sync = MagicMock()
        sync.create_pull_request_from_task.return_value = None  # falsy = failed
        with patch("github_utils.get_github_sync", return_value=sync):
            r = self.engine._func_create_merge_request({})
        self.assertTrue(r["success"])  # still non-fatal
        self.assertEqual(r["action"], "failed")

    def test_github_created_pr_reports_created(self):
        self._write_config("github")
        sync = MagicMock()
        sync.create_pull_request_from_task.return_value = {"number": 7, "html_url": "u"}
        with patch("github_utils.get_github_sync", return_value=sync):
            r = self.engine._func_create_merge_request({})
        self.assertTrue(r["success"])
        self.assertEqual(r["action"], "created")
        self.assertEqual(r["provider"], "github")

    def test_github_exception_is_non_fatal_failed(self):
        self._write_config("github")
        sync = MagicMock()
        sync.create_pull_request_from_task.side_effect = Exception("boom")
        with patch("github_utils.get_github_sync", return_value=sync):
            r = self.engine._func_create_merge_request({})
        self.assertTrue(r["success"])  # non-fatal
        self.assertEqual(r["action"], "failed")
        self.assertIn("boom", r["error"])

    def test_gitlab_sync_none_reports_skipped_not_unimplemented(self):
        # get_gitlab_sync now returns None on not-enabled/missing-token (unified
        # factory contract, m-provider-layer-dedup) — the func must report
        # skipped+reason, NOT fall through to the bare "failed" except handler.
        self._write_config("gitlab")
        with patch("gitlab_utils.get_gitlab_sync", return_value=None):
            r = self.engine._func_create_merge_request({})
        self.assertTrue(r["success"])
        self.assertEqual(r["action"], "skipped")
        self.assertEqual(r["provider"], "gitlab")
        self.assertIn("token", r["reason"].lower())
        self.assertNotIn("not implemented", json.dumps(r).lower())

    def test_jira_reports_not_applicable_not_unimplemented(self):
        self._write_config("jira")
        r = self.engine._func_create_merge_request({})
        self.assertTrue(r["success"])
        self.assertEqual(r["action"], "skipped")
        self.assertIn("not applicable", r["message"].lower())
        self.assertNotIn("not implemented", r["message"].lower())

    def test_failed_mr_does_not_abort_completion_chain(self):
        """codex: a failed PR/MR (success=True, action=failed) must NOT stop the
        chain — cleanup/clear/checkout still run."""
        ran = []

        def ok(name):
            def f(args=None):
                ran.append(name)
                return {"func": name, "success": True}
            return f

        chain = [
            ("create_merge_request",
             lambda args=None: {"func": "create_merge_request", "success": True, "action": "failed"}),
            ("cleanup_task_scoped_state", ok("cleanup_task_scoped_state")),
            ("clear_task_state", ok("clear_task_state")),
            ("checkout_default_branch", ok("checkout_default_branch")),
        ]
        result = self.engine._run_completion_chain("provider", chain, {})
        self.assertTrue(result["success"])
        self.assertEqual(ran, ["cleanup_task_scoped_state", "clear_task_state", "checkout_default_branch"])


if __name__ == "__main__":
    main()
