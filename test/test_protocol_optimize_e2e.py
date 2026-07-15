#!/usr/bin/env python3
"""End-to-end wiring tests for the `optimize` protocol (T4).

Exercises the full state-machine chain (setup → metric-script → baseline →
experimentation × N → synthesis) under realistic conditions, with
subprocess.run mocked at the metric / git boundaries. Verifies:

* `_func_write_optimize_setup` persists user settings; subsequent funcs read them.
* `_func_validate_metric_script` double-run gate enforces stability.
* `_func_capture_metric_baseline` + `_func_check_cost_estimate` order: cost
  projection requires baseline timing.
* Batched experimentation loop with `batch_size=2`: 4 iterations advance
  through 1 mid-batch checkpoint.
* `_func_check_termination` exits the loop on `max_iterations`.
* `_func_policy_compliance_audit` runs in synthesis without blocking.
* `optimize.json` config is loadable and exposes 7 named steps in order.

Completion-step squash + leaderboard behaviour is covered by
`test/test_protocol_engine_completion_optimize.py` and not duplicated here.

Run with: python3 -m pytest test/test_protocol_optimize_e2e.py -v
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import MagicMock, patch

TEST_DIR = Path(__file__).parent
PROJECT_ROOT = TEST_DIR.parent
HOOKS_DIR = PROJECT_ROOT / "plugin" / "hooks"
sys.path.insert(0, str(HOOKS_DIR))


def _make_completed(returncode=0, stdout="", stderr=""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


class OptimizeE2EBase(TestCase):
    """Fixture for the optimize-protocol e2e wiring tests.

    Lays out a temp project with current_task.json + task file already in
    `optimize` protocol state at step 3 (experimentation), loop_iteration=0.
    Tests drive each func directly and assert on state transitions.
    """

    TASK_NAME = "o-line-count"
    BRANCH = "optimize/line-count"

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
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
                "loop_iteration": 0,
                "experimentation_started_at": "2026-05-05T00:00:00+00:00",
            },
        })
        self._write_json(self.temp_dir / ".claude" / "state" / "daic-mode.json", {
            "mode": "implementation",
        })

        # Task file with optimize.baseline_commit slot
        task_file = self.temp_dir / "team-management" / "tasks" / f"{self.TASK_NAME}.md"
        task_file.write_text(
            "---\n"
            f"task: {self.TASK_NAME}\n"
            f"branch: {self.BRANCH}\n"
            "status: in-progress\n"
            "created: 2026-05-05\n"
            "---\n\n"
            "# Line Count Optimization\n\n"
            "## Success Criteria\n- [ ] Reduce line count\n",
            encoding="utf-8",
        )

        # Copy the real optimize.json into the test fixture so the engine can load it
        real_config = PROJECT_ROOT / "plugin" / "protocol-configs" / "optimize.json"
        target_config = self.temp_dir / "team-management" / "protocol-configs" / "system" / "optimize.json"
        shutil.copy(real_config, target_config)

        # Patch shared_state globals so the engine reads from temp_dir
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

    def _set_loop_iter(self, iter_value: int):
        state = self._read_json(self.temp_dir / ".claude" / "state" / "current_task.json")
        state.setdefault("protocol", {})["loop_iteration"] = iter_value
        self._write_json(self.temp_dir / ".claude" / "state" / "current_task.json", state)

    def _state_path(self) -> Path:
        return self.temp_dir / ".claude" / "state" / "optimize-state.json"

    def _tsv_path(self) -> Path:
        return self.temp_dir / "team-management" / "tasks" / self.TASK_NAME / "results.tsv"

    def _git_router(self, *, dirty=False, head="abc1234"):
        """side_effect router for protocol_engine.subprocess.run handling
        `git status --porcelain` (dirty-tree gate) and
        `git rev-parse --short HEAD` (commit_sha fallback). Uses contains-check
        rather than fixed argv positions so that engine wrappers like
        `git -c core.quotePath=false status …` route correctly."""
        def router(argv, **kwargs):
            if isinstance(argv, list) and "status" in argv and "--porcelain" in argv:
                return _make_completed(returncode=0, stdout=("M tracked.py\n" if dirty else ""))
            if isinstance(argv, list) and "rev-parse" in argv:
                return _make_completed(returncode=0, stdout=f"{head}\n")
            raise AssertionError(f"Unexpected subprocess invocation: {argv}")
        return router

    def _ok_metric(self, metric_value, run_count=1, wall_clock_s=0.1, aggregator="median"):
        """Build a successful _func_run_metric return dict for patch.object."""
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


class TestOptimizeJsonLoadable(OptimizeE2EBase):
    """The shipped optimize.json is parseable and exposes 7 ordered steps."""

    def test_optimize_protocol_loadable_and_well_shaped(self):
        config_path = self.temp_dir / "team-management" / "protocol-configs" / "system" / "optimize.json"
        config = self._read_json(config_path)
        self.assertEqual(config["name"], "optimize")
        steps = [s["name"] for s in config["steps"]]
        self.assertEqual(steps, [
            "setup", "metric-script", "baseline",
            "experimentation", "synthesis", "code-review", "completion",
        ])
        # experimentation must be looping_step
        exp = next(s for s in config["steps"] if s["name"] == "experimentation")
        self.assertTrue(exp.get("looping_step"))
        # setup must have validate_optimize_setup FIRST (no-side-effect gate)
        # and write_optimize_setup LAST (after task/branch/issue scaffolding).
        # This split prevents both classes of lifecycle bug:
        # - Validation failure with side effects already created (write-last
        #   alone leaves branch/task/issue created on bad input)
        # - Stale optimize-state.json from a downstream failure (write-first
        #   alone leaves the file behind if a later post_func errors)
        setup = next(s for s in config["steps"] if s["name"] == "setup")
        self.assertEqual(setup["post_funcs"][0], "validate_optimize_setup")
        self.assertEqual(setup["post_funcs"][-1], "write_optimize_setup")
        # baseline must have capture_metric_baseline before check_cost_estimate
        baseline = next(s for s in config["steps"] if s["name"] == "baseline")
        idx_capture = baseline["post_funcs"].index("capture_metric_baseline")
        idx_cost = baseline["post_funcs"].index("check_cost_estimate")
        self.assertLess(idx_capture, idx_cost)
        # experimentation post_funcs in correct order: log → update_best → check_term → batch_checkpoint
        self.assertEqual(exp["post_funcs"], [
            "log_experiment_result", "update_best_commit",
            "check_termination", "batch_checkpoint",
        ])

    def test_protocol_list_includes_optimize(self):
        result = self.engine.list_protocols()
        names = [p["name"] for p in result["protocols"]]
        self.assertIn("optimize", names)
        opt = next(p for p in result["protocols"] if p["name"] == "optimize")
        self.assertEqual(opt["steps_count"], 7)


class TestOptimizeFullChain(OptimizeE2EBase):
    """Drive the post_funcs chain step-by-step: setup → metric-script →
    baseline → experimentation × 4 (with mid-batch checkpoint) → synthesis."""

    def test_setup_writes_state(self):
        # Step 1: setup post_func write_optimize_setup
        result = self.engine._func_write_optimize_setup({
            "metric_command": "python3 metric.py",
            "metric_parser": r"^([0-9.]+)$",
            "metric_direction": "min",
            "metric_monotonic": True,
            "max_iterations": 4,
            "runs_per_iteration": 1,
            "aggregator": "median",
            "batch_size": 2,
            "frozen_paths": [],
            "env_pass": [],
        })
        self.assertTrue(result["success"], result)
        state = self._read_json(self._state_path())
        self.assertEqual(state["batch_size"], 2)
        self.assertEqual(state["max_iterations"], 4)
        self.assertEqual(state["metric_direction"], "min")

    def test_full_chain_setup_to_synthesis(self):
        # === Step 1: setup ===
        # max_duration=None: this test exercises max_iterations termination.
        # The fixture seeds experimentation_started_at at 2026-05-05T00:00, so
        # leaving the default "8h" max_duration would correctly trip the
        # wall-clock cap (now real >> seeded) and short-circuit the test.
        self.engine._func_write_optimize_setup({
            "metric_command": "python3 metric.py",
            "metric_parser": r"^([0-9.]+)$",
            "metric_direction": "min",
            "metric_monotonic": True,
            "max_iterations": 4,
            "max_duration": None,
            "runs_per_iteration": 1,
            "aggregator": "median",
            "stability_threshold_pct": 5.0,
            "batch_size": 2,
        })

        # === Step 2: metric-script — validate (double-run stability) ===
        with patch("protocol_engine.subprocess.run",
                  side_effect=[_make_completed(stdout="10.0\n"),
                               _make_completed(stdout="10.0\n")]):
            r = self.engine._func_validate_metric_script()
            self.assertTrue(r["success"], r)

        # === Step 3: baseline — capture + cost estimate ===
        with patch("protocol_engine.subprocess.run", side_effect=[
            _make_completed(stdout="10.0\n"),                # metric run
            _make_completed(stdout="abc1234\n"),             # git rev-parse for baseline_commit
        ]):
            r = self.engine._func_capture_metric_baseline()
            self.assertTrue(r["success"], r)
            self.assertEqual(r["baseline_metric"], 10.0)

        # cost estimate: bounded → no ack needed
        r = self.engine._func_check_cost_estimate()
        self.assertTrue(r["success"], r)
        self.assertIn("projection", r)
        self.assertIn("projected_wall_clock_s", r["projection"])
        self.assertEqual(r["projection"]["max_iterations"], 4)
        self.assertEqual(r["projection"]["runs_per_iteration"], 1)

        # === Step 4: experimentation, batch_size=2, max_iterations=4 ===
        # Per-iteration: log_experiment_result drives _func_run_metric (mocked
        # to return the iteration's target metric_value) and gates on a clean
        # working tree (router returns empty stdout for `git status`).

        # Iter 0
        self._set_loop_iter(0)
        with patch.object(self.engine, "_func_run_metric",
                          return_value=self._ok_metric(9.0)):
            with patch("protocol_engine.subprocess.run",
                       side_effect=self._git_router(head="abc1234")):
                log_r = self.engine._func_log_experiment_result({
                    "commit_sha": "abc1234", "hypothesis": "h0",
                })
        self.assertTrue(log_r["success"], log_r)
        with patch("protocol_engine.subprocess.run",
                   return_value=_make_completed(stdout="abc1234\n")):
            best_r = self.engine._func_update_best_commit()
        self.assertTrue(best_r["success"])
        term_r = self.engine._func_check_termination()
        self.assertFalse(term_r.get("terminate", False))
        # iter 0: (0+1) % 2 = 1 → mid-batch → no checkpoint
        bc_r = self.engine._func_batch_checkpoint()
        self.assertTrue(bc_r["success"], bc_r)
        self.assertFalse(bc_r.get("awaiting_approval", False))

        # Iter 1: at boundary
        self._set_loop_iter(1)
        with patch.object(self.engine, "_func_run_metric",
                          return_value=self._ok_metric(8.5)):
            with patch("protocol_engine.subprocess.run",
                       side_effect=self._git_router(head="def5678")):
                self.engine._func_log_experiment_result({
                    "commit_sha": "def5678", "hypothesis": "h1",
                })
        with patch("protocol_engine.subprocess.run",
                   return_value=_make_completed(stdout="def5678\n")):
            self.engine._func_update_best_commit()
        # batch_checkpoint at iter 1: (1+1)%2 = 0 → boundary → blocks
        bc_r = self.engine._func_batch_checkpoint()
        self.assertFalse(bc_r["success"])
        self.assertTrue(bc_r.get("awaiting_approval"))
        # Now approve
        bc_r = self.engine._func_batch_checkpoint({"approve_next_batch": True})
        self.assertTrue(bc_r["success"])
        self.assertTrue(bc_r.get("approved"))

        # Iters 2 + 3: same pattern, terminate at iter 3 via max_iterations=4
        for i, (m, sha, hyp) in enumerate([(8.0, "ghi9012", "h2"), (7.5, "jkl3456", "h3")], start=2):
            self._set_loop_iter(i)
            with patch.object(self.engine, "_func_run_metric",
                              return_value=self._ok_metric(m)):
                with patch("protocol_engine.subprocess.run",
                           side_effect=self._git_router(head=sha)):
                    self.engine._func_log_experiment_result({
                        "commit_sha": sha, "hypothesis": hyp,
                    })
            with patch("protocol_engine.subprocess.run",
                       return_value=_make_completed(stdout=f"{sha}\n")):
                self.engine._func_update_best_commit()
        # After iter 3, loop_iteration is 3; max_iterations=4 means terminate when
        # the protocol engine's _handle_loop_iteration would set loop_iteration=4
        # before the next pre_funcs run. Simulate that:
        self._set_loop_iter(4)
        term_r = self.engine._func_check_termination()
        self.assertTrue(term_r.get("terminate"), term_r)
        self.assertEqual(term_r.get("reason"), "max_iterations")

        # Verify TSV grew with 4 ok rows + 1 summary row from the terminate
        tsv_content = self._tsv_path().read_text()
        self.assertEqual(tsv_content.count("\tok\t"), 4)
        self.assertEqual(tsv_content.count("\tsummary\t"), 1)

        # Verify best_commit recorded the lowest metric (direction=min)
        task_md = (self.temp_dir / "team-management" / "tasks" / f"{self.TASK_NAME}.md").read_text()
        self.assertIn("optimize.best_metric: 7.5", task_md)
        self.assertIn("optimize.best_commit: jkl3456", task_md)

        # === Step 5: synthesis — policy_compliance_audit (non-blocking) ===
        with patch("protocol_engine.subprocess.run") as mock_run:
            # git log returns a clean history (no frozen-path edits, no tsv edits, no metric_value strings)
            mock_run.return_value = _make_completed(stdout="abc1234 commit msg\nfile1.py\n")
            audit_r = self.engine._func_policy_compliance_audit()
            self.assertTrue(audit_r["success"], audit_r)
            # metric_gaming_flags is informational; the field exists
            self.assertIn("metric_gaming_flags", audit_r)


class TestOptimizeAdvanceArgs(OptimizeE2EBase):
    """Regression: optimize.json advance_args declarations must NOT mark
    optional/conditional fields as required. The engine's _validate_advance_args
    treats every entry as required AND truthy, so a too-eager declaration
    breaks the documented happy paths (unbounded mode, batched-loop exit,
    bounded baseline, etc.).
    """

    def test_setup_advance_args_only_lists_required_keys(self):
        config_path = self.temp_dir / "team-management" / "protocol-configs" / "system" / "optimize.json"
        config = self._read_json(config_path)
        setup = next(s for s in config["steps"] if s["name"] == "setup")
        # Only genuinely required fields belong here. Optional/nullable knobs
        # (max_iterations, target_metric, frozen_paths, etc.) are inspected
        # by _func_validate_optimize_setup with default fallbacks — they
        # must NOT appear in advance_args, otherwise null and falsy values
        # make valid configurations unreachable.
        self.assertEqual(set(setup["advance_args"]), {
            "task", "branch", "task_content",
            "metric_command", "metric_parser", "metric_direction", "metric_monotonic",
        })

    def test_baseline_step_no_advance_args(self):
        config_path = self.temp_dir / "team-management" / "protocol-configs" / "system" / "optimize.json"
        config = self._read_json(config_path)
        baseline = next(s for s in config["steps"] if s["name"] == "baseline")
        # unbounded_acknowledged is conditional (only required when both
        # max_iterations and max_duration are null). Listing it as required
        # would block the bounded-mode happy path.
        self.assertEqual(baseline.get("advance_args", []), [])

    def test_experimentation_step_no_advance_args(self):
        config_path = self.temp_dir / "team-management" / "protocol-configs" / "system" / "optimize.json"
        config = self._read_json(config_path)
        exp = next(s for s in config["steps"] if s["name"] == "experimentation")
        # exit_loop, restart, approve_next_batch, metric_value, commit_sha,
        # hypothesis are all conditional. The boolean controls (exit_loop,
        # restart, approve_next_batch) are especially fatal because False
        # counts as missing — there'd be no valid call shape that satisfies
        # the validator while staying in the loop.
        self.assertEqual(exp.get("advance_args", []), [])


class TestOptimizeBatchCheckpointModuloE2E(OptimizeE2EBase):
    """Verify that with batch_size=3, checkpoints fire at iter 2, 5, 8 (and not in between)."""

    def test_batch_size_3_fires_at_correct_boundaries(self):
        from shared_state import write_optimize_state
        write_optimize_state({
            "metric_direction": "min", "batch_size": 3,
            "metric_command": "python3 m.py", "metric_parser": r"^([0-9.]+)$",
            "metric_monotonic": True, "max_iterations": 100,
        })
        # Seed minimal TSV
        header = "iteration\ttimestamp\tcommit_sha\tmetric_value\trun_count\taggregator\twall_clock_s\tstatus\thypothesis\n"
        self._tsv_path().write_text(header, encoding="utf-8")

        # iter 0, 1: mid-batch (no-op)
        for i in (0, 1):
            self._set_loop_iter(i)
            r = self.engine._func_batch_checkpoint()
            self.assertTrue(r["success"], f"iter {i}: {r}")
            self.assertFalse(r.get("awaiting_approval", False))

        # iter 2: boundary (blocks)
        self._set_loop_iter(2)
        r = self.engine._func_batch_checkpoint()
        self.assertFalse(r["success"])
        self.assertTrue(r.get("awaiting_approval"))

        # iter 3, 4: mid-batch
        for i in (3, 4):
            self._set_loop_iter(i)
            r = self.engine._func_batch_checkpoint()
            self.assertTrue(r["success"], f"iter {i}: {r}")

        # iter 5: boundary
        self._set_loop_iter(5)
        r = self.engine._func_batch_checkpoint()
        self.assertFalse(r["success"])
        self.assertTrue(r.get("awaiting_approval"))


if __name__ == "__main__":
    main()
