"""T3 Phase 1.5 Spike — End-to-end validation of looping_step mechanisms.

Builds on LoopProtocolTestBase from test_protocol_engine_loop.py. Adds three
checks that the existing T2 unit tests don't cover end-to-end:

1. test_5_iteration_loop_with_state_inspection (criteria 1+2): runs the spec'd
   5 iterations and inspects current_task.json directly per step.
2. test_pre_compact_subprocess_then_resume (criterion 3): invokes pre-compact.py
   as an actual subprocess so the checkpoint write path is exercised, then
   discards and re-instantiates the engine to validate cross-session resume.
3. test_force_safe_bypass_in_audit_log (criterion 6): asserts the bypass marker
   appears in protocol-logs/<task>.json (the existing test only checks step text).

Plus the token-growth report:

4. test_token_growth_report (criterion 7): snapshots tokens per state file
   across 5 iterations, fits a linear regression, projects to 50 iterations
   under worst-case and post-compact models, asserts worst_case_50 stays
   under the 850K threshold (the GO verdict guard). Numerical results are
   recorded in the task work log Verdict block.

Criteria 4 and 5 are fully covered by existing LoopProtocolTestBase tests
(test_experimentation_started_at_persists_across_iterations,
test_restart_archives_results_tsv, test_restart_clears_optimize_best_commit_from_frontmatter,
test_restart_resets_experimentation_started_at) — not duplicated here.

Run with: python3 -m pytest test/test_protocol_engine_spike.py -v
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import main

TEST_DIR = Path(__file__).parent
PROJECT_ROOT = TEST_DIR.parent
HOOKS_DIR = PROJECT_ROOT / "plugin" / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

from test_protocol_engine_loop import LoopProtocolTestBase


class TestSpikeFullLoop(LoopProtocolTestBase):
    """Criteria 1+2: 5-iteration loop end-to-end with loop_iteration visible
    in current_task.json after every iteration."""

    def test_5_iteration_loop_with_state_inspection(self):
        result = self.engine.start_protocol("looptest")
        self.assertTrue(result["success"])
        for i in range(5):
            r = self.engine.advance_step(f"iter {i}")
            self.assertTrue(r["success"], r)
            self.assertTrue(r.get("looped"), f"iter {i} did not loop")
            self.assertEqual(r["loop_iteration"], i + 1)
            state = self._get_protocol_state()
            self.assertEqual(state["loop_iteration"], i + 1,
                             f"current_task.json loop_iteration mismatch at iter {i}")
        # Exit loop — must advance to "done"
        r = self.engine.advance_step("exit", args={"exit_loop": True})
        self.assertTrue(r["success"])
        self.assertFalse(r.get("looped", False))
        self.assertEqual(r["step"]["name"], "done")


class TestSpikePreCompactSubprocess(LoopProtocolTestBase):
    """Criterion 3: invoke pre-compact.py as a real subprocess, verify the
    checkpoint file, then drop and re-instantiate the engine to confirm
    cross-session auto-resume from the recorded loop_iteration."""

    def test_pre_compact_subprocess_then_resume(self):
        self.engine.start_protocol("looptest")
        for i in range(3):
            self.engine.advance_step(f"iter {i}")
        # Sanity: state file shows 3 iterations completed
        self.assertEqual(self._get_protocol_state().get("loop_iteration"), 3)

        # Invoke pre-compact.py as subprocess against the temp project.
        # cwd matters: shared_state.get_project_root walks up from CWD looking
        # for .claude (CLAUDE_PROJECT_DIR is NOT honored in the lookup).
        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(self.temp_dir)}
        cp = subprocess.run(
            [sys.executable, str(HOOKS_DIR / "pre-compact.py")],
            input='{"trigger":"auto"}',
            capture_output=True,
            text=True,
            env=env,
            cwd=str(self.temp_dir),
            timeout=10,
        )
        # pre-compact.py must exit 0. PreCompact has nothing to block
        # (hooks-reference.md exit-code-2 table: "N/A, shows stderr to user
        # only"), and the harness treats a non-zero PreCompact exit as a hook
        # failure that errors out /compact. Exact-match so a regression that
        # reintroduces `sys.exit(2)` surfaces as a test failure.
        self.assertEqual(cp.returncode, 0,
                         f"pre-compact.py contract change: rc={cp.returncode} stderr={cp.stderr}")
        compact_flag = self.temp_dir / ".claude" / "state" / "compact-pending.flag"
        self.assertTrue(compact_flag.exists(),
                        "pre-compact.py did not write checkpoint flag")
        checkpoint = json.loads(compact_flag.read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["protocol"]["loop_iteration"], 3)
        self.assertEqual(checkpoint["task"], self.TASK_NAME)
        self.assertEqual(checkpoint["branch"], self.BRANCH)

        # Simulate post-compact: drop in-memory engine, re-instantiate from disk
        del self.engine
        from protocol_engine import ProtocolEngine
        self.engine = ProtocolEngine(self.temp_dir)

        r = self.engine.start_protocol("looptest")
        self.assertTrue(r["success"])
        self.assertTrue(r.get("resumed"),
                        "engine did not auto-resume after subprocess pre-compact")
        self.assertEqual(r["loop_iteration"], 3)

        # Continue the loop — must reach iter 5 without resetting
        for i in range(3, 5):
            r2 = self.engine.advance_step(f"iter {i}")
            self.assertTrue(r2["success"])
            self.assertEqual(r2["loop_iteration"], i + 1)


class TestSpikeForceSafeAuditLog(LoopProtocolTestBase):
    """Criterion 6: resume_force_safe=true bypass marker visible in the
    task's protocol-logs/<task>.json audit log (the existing
    test_resume_force_safe_bypasses_scan only checks the step start text)."""

    def test_force_safe_bypass_in_audit_log(self):
        self.engine.start_protocol("looptest")
        self.engine.advance_step("iter 1")

        # Inject JWT into results.tsv to trigger credential scan
        tsv = self._task_dir() / "results.tsv"
        tsv.write_text(
            "iteration\thypothesis\n"
            "1\teyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdef0123456789\n",
            encoding="utf-8",
        )

        # Without force-safe: must abort
        r = self.engine.start_protocol("looptest")
        self.assertFalse(r["success"])
        self.assertTrue(r.get("resume_blocked"))

        # With force-safe: must succeed
        r2 = self.engine.start_protocol("looptest", resume_force_safe=True)
        self.assertTrue(r2["success"])
        self.assertTrue(r2.get("resumed"))

        # Bypass marker must appear in the audit log (per design at
        # protocol_engine.py:1161-1178 — note text "credential scan BYPASSED").
        # Scan all log files in protocol-logs/: the looptest synthetic protocol
        # never runs set_task_state, so the engine keeps the log at
        # _pending.json instead of renaming it to <task>.json.
        logs_dir = self.temp_dir / ".claude" / "state" / "protocol-logs"
        log_files = sorted(logs_dir.glob("*.json"))
        self.assertGreater(len(log_files), 0, "no audit log files found")
        notes_blob = ""
        for lf in log_files:
            data = json.loads(lf.read_text(encoding="utf-8"))
            notes_blob += json.dumps(data.get("notes", []))
        self.assertIn("resume_force_safe=true", notes_blob,
                      "force-safe bypass marker not found in audit log notes")
        self.assertIn("BYPASSED", notes_blob,
                      "credential-scan bypass keyword not found in audit log notes")


class TestSpikeTokenGrowth(LoopProtocolTestBase):
    """Criterion 7: snapshot token usage per iteration across 5 iterations,
    extrapolate to 50 iterations under both linear and post-compact models,
    write the markdown report, and assign the GO/NO-GO verdict."""

    THRESHOLD = 850_000  # Opus 4.7 1M context * 85% auto-compact

    def test_token_growth_report(self):
        try:
            import tiktoken
            enc = tiktoken.get_encoding("cl100k_base")
        except ImportError:
            self.skipTest("tiktoken not available; install with `pip install tiktoken`")

        # CLAUDE.md is auto-loaded by Claude Code itself (not by session-start.py)
        # — measured here as the static project-context baseline.
        claude_md_path = PROJECT_ROOT / "CLAUDE.md"
        claude_md_tokens = (
            len(enc.encode(claude_md_path.read_text(encoding="utf-8")))
            if claude_md_path.exists() else 0
        )

        self.engine.start_protocol("looptest")
        snapshots = []
        for i in range(5):
            self.engine.advance_step(f"iter {i}")
            ts_text = (self.temp_dir / ".claude/state/current_task.json").read_text(encoding="utf-8")
            tsv_path = self._task_dir() / "results.tsv"
            tsv_text = tsv_path.read_text(encoding="utf-8") if tsv_path.exists() else ""
            # The looptest protocol doesn't run set_task_state, so the audit
            # log stays as _pending.json. Sum all *.json log files. NOTE:
            # session-start.py does NOT inject the audit log (verified at
            # plugin/hooks/session-start.py:135-145 — only task state
            # + task file are injected). Audit log is reported separately as
            # the engine-bookkeeping cost; it only enters the conversation
            # when an agent calls protocol_log().
            logs_dir = self.temp_dir / ".claude/state/protocol-logs"
            audit_text = "".join(
                lf.read_text(encoding="utf-8") for lf in sorted(logs_dir.glob("*.json"))
            )
            task_md_text = (self.temp_dir / "team-management/tasks"
                            / f"{self.TASK_NAME}.md").read_text(encoding="utf-8")
            per_iter = {
                "iteration": i,
                "current_task_json_tokens": len(enc.encode(ts_text)),
                "results_tsv_tokens": len(enc.encode(tsv_text)),
                "audit_log_tokens": len(enc.encode(audit_text)),
                "task_md_tokens": len(enc.encode(task_md_text)),
                "claude_md_tokens": claude_md_tokens,
            }
            # session-start.py injects: task_state JSON + task file content
            # + protocol step start/end text. CLAUDE.md is auto-loaded by
            # Claude Code itself. Audit log is NOT in this payload.
            per_iter["session_start_tokens"] = (
                per_iter["current_task_json_tokens"]
                + per_iter["task_md_tokens"]
                + per_iter["claude_md_tokens"]
            )
            snapshots.append(per_iter)

        # Compute regression + projections inline. These numbers are the
        # spike's deliverable and are recorded in the task work log Verdict
        # block; the test asserts the verdict (GO) holds as a regression gate.
        n = len(snapshots)
        xs = [s["iteration"] for s in snapshots]
        # Guard against degenerate inputs (single point, all-equal xs) — the
        # synthetic loop always uses range(5) so this is defensive only,
        # but a future maintainer changing the loop count to 1 deserves an
        # explicit error rather than a silent slope=0 fallback.
        self.assertGreaterEqual(n, 2, "regression undefined: need ≥2 snapshots")
        self.assertGreaterEqual(len(set(xs)), 2,
                                "regression undefined: need ≥2 distinct iteration indices")
        ys = [s["session_start_tokens"] for s in snapshots]
        ys_audit = [s["audit_log_tokens"] for s in snapshots]
        x_mean = sum(xs) / n
        y_mean = sum(ys) / n
        y_audit_mean = sum(ys_audit) / n
        den = sum((x - x_mean) ** 2 for x in xs)
        slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / den
        intercept = y_mean - slope * x_mean
        slope_audit = sum((x - x_mean) * (y - y_audit_mean)
                          for x, y in zip(xs, ys_audit)) / den
        intercept_audit = y_audit_mean - slope_audit * x_mean

        linear_50 = slope * 50 + intercept
        audit_50 = slope_audit * 50 + intercept_audit
        # Worst case: session-start re-injection cost + full audit log loaded
        # via protocol_log(), no auto-compact runs.
        worst_case_50 = linear_50 + audit_50
        # Auto-compact runs once at iter 25 with ~30% retention on the
        # audit-log proxy for conversation transcript.
        post_compact_50 = max(0.0, worst_case_50 - 0.7 * audit_50)

        # Sanity assertions — engine bookkeeping is positive but bounded.
        self.assertGreater(snapshots[-1]["audit_log_tokens"],
                           snapshots[0]["audit_log_tokens"],
                           "audit log did not grow across iterations")
        self.assertGreater(slope_audit, 0, "audit_log slope must be positive")
        # Verdict regression gate — protects the GO decision.
        self.assertLessEqual(
            worst_case_50, self.THRESHOLD,
            f"worst_case_50 ({worst_case_50:.0f}) exceeded threshold "
            f"({self.THRESHOLD}) — verdict has degraded from GO; "
            f"re-evaluate against the criteria in the task work log."
        )
        self.assertLessEqual(post_compact_50, self.THRESHOLD)


if __name__ == "__main__":
    main()
