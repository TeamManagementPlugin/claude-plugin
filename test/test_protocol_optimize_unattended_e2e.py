#!/usr/bin/env python3
"""End-to-end wiring tests for the `optimize-unattended` protocol (T5).

The autonomous twin of `optimize`. Reuses the same engine plumbing and func set
as T4 — the only delta is the experimentation step's `post_funcs` (no
`batch_checkpoint`) and the `start` reference (`optimize-experimentation-auto.md`).
These tests verify the four success-criteria scenarios specific to T5:

* `max_iterations=3` terminates and the engine has no batch_checkpoint to gate the advance.
* `regression_halt_n=2` terminates after two consecutive worsenings against best.
* `policy_compliance_audit` (in synthesis) emits a `frozen_path_modified` flag
  on a synthesized git history that touches a frozen path — non-blocking.
* Auto-resume credential scan blocks `protocol_start` on an AWS access key in
  resume-stdout-tail.txt; `resume_force_safe=True` bypasses and records a note.

Run with: python3 -m pytest test/test_protocol_optimize_unattended_e2e.py -v
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


class OptimizeUnattendedE2EBase(TestCase):
    """Fixture for the optimize-unattended e2e wiring tests.

    Mirrors `OptimizeE2EBase` (test_protocol_optimize_e2e.py) but uses the
    `optimize-unattended` protocol config and pre-seeds protocol state with
    `name="optimize-unattended"` so auto-resume / completion routing matches.
    """

    TASK_NAME = "o-line-count-unattended"
    BRANCH = "optimize/line-count-unattended"

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
                "name": "optimize-unattended",
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
            "# Line Count Optimization (Unattended)\n\n"
            "## Success Criteria\n- [ ] Reduce line count\n",
            encoding="utf-8",
        )

        # Copy the real optimize-unattended.json into the test fixture so the engine can load it
        real_config = PROJECT_ROOT / "plugin" / "protocol-configs" / "optimize-unattended.json"
        target_config = self.temp_dir / "team-management" / "protocol-configs" / "system" / "optimize-unattended.json"
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

    def _task_dir(self) -> Path:
        return self.temp_dir / "team-management" / "tasks" / self.TASK_NAME

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


class TestUnattendedConfigShape(OptimizeUnattendedE2EBase):
    """The shipped optimize-unattended.json is a D3 hybrid of optimize.json:
    same 7 ordered steps, but the experimentation step has no batch_checkpoint
    and references the auto sub-protocol."""

    def test_config_loadable_with_correct_name_and_step_count(self):
        config_path = self.temp_dir / "team-management" / "protocol-configs" / "system" / "optimize-unattended.json"
        config = self._read_json(config_path)
        self.assertEqual(config["name"], "optimize-unattended")
        steps = [s["name"] for s in config["steps"]]
        self.assertEqual(steps, [
            "setup", "metric-script", "baseline",
            "experimentation", "synthesis", "code-review", "completion",
        ])

    def test_experimentation_step_has_no_batch_checkpoint(self):
        config_path = self.temp_dir / "team-management" / "protocol-configs" / "system" / "optimize-unattended.json"
        config = self._read_json(config_path)
        exp = next(s for s in config["steps"] if s["name"] == "experimentation")
        self.assertTrue(exp.get("looping_step"))
        # Critical D3 hybrid invariant: post_funcs is the autonomous chain
        self.assertEqual(exp["post_funcs"], [
            "log_experiment_result", "update_best_commit", "check_termination",
        ])
        self.assertNotIn("batch_checkpoint", exp["post_funcs"])
        self.assertEqual(exp["start"], "@sub-protocols/optimize-experimentation-auto.md")

    def test_protocol_list_includes_optimize_unattended(self):
        result = self.engine.list_protocols()
        names = [p["name"] for p in result["protocols"]]
        self.assertIn("optimize-unattended", names)
        opt = next(p for p in result["protocols"] if p["name"] == "optimize-unattended")
        self.assertEqual(opt["steps_count"], 7)


class TestUnattendedMaxIterationsTerminates(OptimizeUnattendedE2EBase):
    """SC: max_iterations=3 → terminates correctly and the engine has no
    batch_checkpoint post_func to gate the auto-advance to synthesis."""

    def test_unattended_max_iterations_terminates_and_advances(self):
        from shared_state import write_optimize_state
        write_optimize_state({
            "metric_command": "python3 m.py",
            "metric_parser": r"^([0-9.]+)$",
            "metric_direction": "min",
            "metric_monotonic": True,
            "max_iterations": 3,
            "max_duration": None,
            "regression_halt_n": None,
            "target_metric": None,
            "runs_per_iteration": 1,
            "aggregator": "median",
            "stability_threshold_pct": 5.0,
            "frozen_paths": [],
            "env_pass": [],
        })

        # Run 3 iterations. After each, log + update_best + check_termination.
        # check_termination should NOT fire on iters 0/1/2 (loop_iter < max_iter=3).
        for i, (m, sha, hyp) in enumerate([
            (10.0, "aaa1111", "h0"),
            (9.0, "bbb2222", "h1"),
            (8.5, "ccc3333", "h2"),
        ]):
            self._set_loop_iter(i)
            with patch.object(self.engine, "_func_run_metric",
                              return_value=self._ok_metric(m)):
                with patch("protocol_engine.subprocess.run",
                           side_effect=self._git_router(head=sha)):
                    log_r = self.engine._func_log_experiment_result({
                        "commit_sha": sha, "hypothesis": hyp,
                    })
                    self.assertTrue(log_r["success"], log_r)
            with patch("protocol_engine.subprocess.run",
                       return_value=_make_completed(stdout=f"{sha}\n")):
                best_r = self.engine._func_update_best_commit()
                self.assertTrue(best_r["success"])
            term_r = self.engine._func_check_termination()
            self.assertFalse(term_r.get("terminate", False),
                             f"iter {i}: should not terminate yet (loop_iter < max_iter)")

        # Engine bumps loop_iter to 3 before the next pre_funcs run.
        self._set_loop_iter(3)
        term_r = self.engine._func_check_termination()
        self.assertTrue(term_r.get("terminate"), term_r)
        self.assertEqual(term_r.get("reason"), "max_iterations")

        # TSV evidence: 3 ok rows + 1 summary row.
        tsv_content = self._tsv_path().read_text()
        self.assertEqual(tsv_content.count("\tok\t"), 3)
        self.assertEqual(tsv_content.count("\tsummary\t"), 1)

        # Best metric is the lowest (direction=min): 8.5 from iter 2.
        task_md = (self._task_dir().parent / f"{self.TASK_NAME}.md").read_text()
        self.assertIn("optimize.best_metric: 8.5", task_md)
        self.assertIn("optimize.best_commit: ccc3333", task_md)

        # Critical: confirm the engine's per-step post_funcs chain has NO
        # batch_checkpoint to interpose between check_termination and the
        # auto-advance. This is the D3 hybrid contract.
        config = self._read_json(
            self.temp_dir / "team-management" / "protocol-configs" / "system" / "optimize-unattended.json"
        )
        exp = next(s for s in config["steps"] if s["name"] == "experimentation")
        self.assertNotIn("batch_checkpoint", exp["post_funcs"])


class TestUnattendedRegressionHaltTerminates(OptimizeUnattendedE2EBase):
    """SC: regression_halt_n=2 → terminates after 2 consecutive worsenings."""

    def test_unattended_regression_halt_terminates(self):
        from shared_state import write_optimize_state
        write_optimize_state({
            "metric_command": "python3 m.py",
            "metric_parser": r"^([0-9.]+)$",
            "metric_direction": "min",
            "metric_monotonic": True,
            "max_iterations": 100,           # high so it doesn't fire first
            "max_duration": None,
            "regression_halt_n": 2,
            "target_metric": None,
            "runs_per_iteration": 1,
            "aggregator": "median",
            "stability_threshold_pct": 5.0,
            "frozen_paths": [],
            "env_pass": [],
        })

        # Iter 0: best = 10.0 (becomes new best)
        # Iter 1: 15.0 — worse than best (1st regression)
        # Iter 2: 20.0 — worse than best (2nd consecutive regression → halt)
        for i, (m, sha, hyp) in enumerate([
            (10.0, "aaa1111", "h0-best"),
            (15.0, "bbb2222", "h1-worse"),
            (20.0, "ccc3333", "h2-worse"),
        ]):
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

        # After iter 2: ok_rows=[10,15,20], last 2 = [15,20], both worse-than-best=10 (min) → halt
        term_r = self.engine._func_check_termination()
        self.assertTrue(term_r.get("terminate"), term_r)
        self.assertEqual(term_r.get("reason"), "regression_halt")

        # TSV summary row written
        tsv_content = self._tsv_path().read_text()
        self.assertEqual(tsv_content.count("\tok\t"), 3)
        self.assertEqual(tsv_content.count("\tsummary\t"), 1)


class TestUnattendedPolicyAuditFrozenPathsViolation(OptimizeUnattendedE2EBase):
    """SC: synthesis policy_compliance_audit emits frozen_path_modified flag
    when a commit between baseline_commit..HEAD touched a frozen path."""

    def test_unattended_frozen_paths_violation_flagged_in_audit(self):
        from shared_state import write_optimize_state
        write_optimize_state({
            "metric_command": "python3 m.py",
            "metric_parser": r"^([0-9.]+)$",
            "metric_direction": "min",
            "metric_monotonic": True,
            "max_iterations": 10,
            "max_duration": None,
            "regression_halt_n": None,
            "target_metric": None,
            "runs_per_iteration": 1,
            "aggregator": "median",
            "stability_threshold_pct": 5.0,
            "frozen_paths": ["src/protected.py"],
            "env_pass": [],
        })

        # Inject baseline_commit into task frontmatter so the audit can scan baseline..HEAD.
        task_md_path = self._task_dir().parent / f"{self.TASK_NAME}.md"
        task_md_path.write_text(
            "---\n"
            f"task: {self.TASK_NAME}\n"
            f"branch: {self.BRANCH}\n"
            "status: in-progress\n"
            "created: 2026-05-05\n"
            "optimize.baseline_commit: bbbbbbb\n"
            "optimize.best_metric: 8.5\n"
            "optimize.best_commit: ccc3333\n"
            "---\n\n"
            "# Line Count Optimization (Unattended)\n\n"
            "## Success Criteria\n- [ ] Reduce line count\n",
            encoding="utf-8",
        )

        # Mock git log --name-only output: a single commit that touched
        # src/protected.py (a frozen path). Format mirrors `git log --name-only`:
        # `<sha> <subject>` then a blank line then the file paths, repeated.
        git_log_output = (
            "evil123 add cache layer to protected module\n"
            "\n"
            "src/protected.py\n"
            "\n"
        )
        # The audit may also call git show / git log --format on individual
        # commits to scan diff lines for hardcoded constants. Provide enough
        # fallthrough output that those don't crash; clean diffs (no `+` lines
        # with the metric value) won't trip the metric_constant_hardcoded check.
        with patch("protocol_engine.subprocess.run") as mock_run:
            mock_run.return_value = _make_completed(stdout=git_log_output)
            audit_r = self.engine._func_policy_compliance_audit()

        self.assertTrue(audit_r["success"], audit_r)
        # Audit is non-blocking by contract — success=True regardless of flags
        self.assertIn("metric_gaming_flags", audit_r)
        flags = audit_r["metric_gaming_flags"]
        # At least one frozen_path_modified flag pointing at src/protected.py
        frozen_flags = [f for f in flags if f.get("kind") == "frozen_path_modified"]
        self.assertTrue(frozen_flags,
                        f"expected at least one frozen_path_modified flag, got: {flags}")
        self.assertTrue(any("src/protected.py" in f.get("file", "") for f in frozen_flags),
                        f"expected src/protected.py in flag.file, got: {frozen_flags}")


class TestUnattendedResumeCredentialScan(OptimizeUnattendedE2EBase):
    """SC: auto-resume aborts when resume-stdout-tail.txt contains a credential
    pattern; resume_force_safe=True bypasses and is recorded in the audit log."""

    def _seed_resume_stdout_with_credential(self) -> Path:
        stdout_tail = self._task_dir() / "resume-stdout-tail.txt"
        # AKIA + 16 [A-Z0-9] chars matches the AWS access key regex
        # AKIAIOSFODNN7EXAMPLE → AKIA + IOSFODNN7EXAMPLE (16 chars). Real
        # public-domain example string from AWS docs; no live credentials here.
        stdout_tail.write_text(
            "iter 5 stdout sample:\n"
            "AKIAIOSFODNN7EXAMPLE access detected in env dump\n"
            "tail end\n",
            encoding="utf-8",
        )
        return stdout_tail

    def _make_protocol_active_with_loop_iter(self):
        """Bump loop_iteration > 0 so start_protocol takes the auto-resume path."""
        state = self._read_json(self.temp_dir / ".claude" / "state" / "current_task.json")
        state["protocol"]["loop_iteration"] = 5
        self._write_json(self.temp_dir / ".claude" / "state" / "current_task.json", state)

    def test_unattended_resume_blocked_by_credential_pattern(self):
        self._make_protocol_active_with_loop_iter()
        self._seed_resume_stdout_with_credential()

        result = self.engine.start_protocol("optimize-unattended")
        self.assertFalse(result.get("success"), result)
        self.assertTrue(result.get("resume_blocked"), result)
        matches = result.get("matches", [])
        self.assertTrue(matches, "expected credential matches list to be non-empty")
        kinds = [m.get("kind") for m in matches]
        self.assertIn("aws_access_key", kinds)

        # resume-blocked.txt was written with redacted findings
        blocked_path = self._task_dir() / "resume-blocked.txt"
        self.assertTrue(blocked_path.exists(), "resume-blocked.txt should be written")
        blocked_text = blocked_path.read_text(encoding="utf-8")
        self.assertIn("aws_access_key", blocked_text)
        # Redaction: full credential should NOT appear in the blocked file
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", blocked_text,
                         "full credential leaked into resume-blocked.txt")

    def test_unattended_resume_force_safe_bypass_succeeds_and_logs(self):
        self._make_protocol_active_with_loop_iter()
        self._seed_resume_stdout_with_credential()

        # Bypass: resume_force_safe=True
        result = self.engine.start_protocol("optimize-unattended", resume_force_safe=True)
        self.assertTrue(result.get("success"), result)

        # Audit log: a note must have been recorded mentioning the bypass.
        # The engine writes resume notes to whichever log file exists for this
        # task — typically `_pending.json` on resume (set_task_state doesn't
        # re-run), or `<task>.json` if a previous func renamed it. Scan all
        # JSON logs in the directory.
        logs_dir = self.temp_dir / ".claude" / "state" / "protocol-logs"
        json_logs = list(logs_dir.glob("*.json"))
        self.assertTrue(json_logs, f"no protocol log files written under {logs_dir}")
        all_notes = []
        for log_path in json_logs:
            log = self._read_json(log_path)
            all_notes.extend(log.get("notes", []))
        bypass_notes = [
            n for n in all_notes
            if "resume_force_safe" in n.get("note", "").lower()
            or "bypass" in n.get("note", "").lower()
        ]
        self.assertTrue(bypass_notes,
                        f"expected at least one note mentioning the bypass, got: {all_notes}")


if __name__ == "__main__":
    main()
