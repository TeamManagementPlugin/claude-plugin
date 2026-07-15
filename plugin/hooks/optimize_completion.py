#!/usr/bin/env python3
"""Optimize + completion funcs — extracted from protocol_engine.py (l-structural-refactors).

Holds OptimizeCompletionMixin: the optimize-protocol machinery (metric run, experiment
logging, termination, leaderboard) and the unified completion dispatcher used by all
protocols. Composed into ProtocolEngine. Imports NOTHING from protocol_engine (one-way
dependency) to avoid an import cycle; calls core helpers (e.g. _validate_run_command,
_func_git_commit) via self through the composed class.
"""

import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from engine_constants import (
    GIT_TIMEOUT_FAST,
    GIT_TIMEOUT_MEDIUM,
    GIT_TIMEOUT_SLOW,
)
from shared_state import (
    get_task_state,
    set_daic_mode,
    get_protocol_state,
    parse_task_frontmatter,
    write_optimize_state,
)
from git_operations import detect_default_branch, validate_branch_name

# metric_parser is a user-supplied regex; bound both the input (characters of
# stdout searched) and the match wall-clock so a catastrophic-backtracking
# pattern cannot hang the MCP server (R2-O2). The match runs in a child
# process because a daemon-thread timeout does NOT work: stdlib `re` holds
# the GIL for the entire C-level match, so the parent's `join(timeout)` never
# wakes up while a runaway match is spinning (verified experimentally).
METRIC_STDOUT_SEARCH_CAP = 262_144  # characters
METRIC_PARSER_TIMEOUT_S = 5.0

# Child source for the guarded match. Pattern and text arrive as JSON via
# stdin (never interpolated into the script — no injection, no argv length
# limits, no text leaking into `ps` output).
_REGEX_CHILD_SRC = (
    "import json, re, sys\n"
    "p = json.loads(sys.stdin.read())\n"
    "try:\n"
    "    m = re.search(p['pattern'], p['text'], re.MULTILINE)\n"
    "except re.error as e:\n"
    "    print(json.dumps({'error': 'metric_parser regex invalid: ' + str(e)}))\n"
    "else:\n"
    "    if m is None:\n"
    "        print(json.dumps({'matched': False}))\n"
    "    else:\n"
    "        try:\n"
    "            g1 = m.group(1)\n"
    "        except IndexError:\n"
    "            g1 = None\n"
    "        print(json.dumps({'matched': True, 'group1': g1}))\n"
)


def _bounded_regex_search(pattern: str, text: str) -> Dict:
    """Run `re.search(pattern, text, re.MULTILINE)` against at most
    METRIC_STDOUT_SEARCH_CAP characters of `text`, in a killable child process
    bounded by METRIC_PARSER_TIMEOUT_S.

    Returns `{"matched": bool, "group1": Optional[str], "error": Optional[str]}`
    — `error` is set for timeout / child failure / no-match-beyond-cap.

    Uses `subprocess.Popen` deliberately (NOT `subprocess.run`): the test
    suites patch `protocol_engine.subprocess.run` with side_effect lists sized
    to the expected git/metric calls, and the guarded match must not consume
    those mocks.
    """
    full_len = len(text)
    capped = text[:METRIC_STDOUT_SEARCH_CAP]
    if full_len > METRIC_STDOUT_SEARCH_CAP:
        # Drop the trailing partial line so a number cut mid-digits cannot
        # silently match as a wrong (truncated) value — e.g. "42.567" capped
        # to "42.5" would otherwise parse cleanly. The dropped line falls
        # into the explicit "truncated to first N characters" error below.
        # Known accepted gap: when the capped prefix contains NO newline at
        # all (e.g. a single-line blob), nothing is dropped and a number
        # straddling exactly the cap boundary can still parse its truncated
        # prefix — fixing that requires match-end-position plumbing from the
        # child and would regress legitimate in-cap matches on giant single
        # lines. Line-structured stdout (the realistic case) is fully covered.
        nl = capped.rfind("\n")
        if nl != -1:
            capped = capped[:nl + 1]
    try:
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", _REGEX_CHILD_SRC],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
    except OSError as e:
        return {"matched": False, "group1": None,
                "error": f"metric_parser match subprocess failed to start: {e}"}
    try:
        out, _ = proc.communicate(
            json.dumps({"pattern": pattern, "text": capped}),
            timeout=METRIC_PARSER_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=GIT_TIMEOUT_FAST)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
        return {"matched": False, "group1": None,
                "error": (
                    f"metric_parser match timed out after {METRIC_PARSER_TIMEOUT_S}s "
                    "and the match process was killed — the pattern likely suffers "
                    "catastrophic backtracking; simplify it"
                )}
    except OSError as e:
        proc.kill()
        return {"matched": False, "group1": None,
                "error": f"metric_parser match subprocess failed: {e}"}
    try:
        payload = json.loads((out or "").strip())
    except ValueError:
        return {"matched": False, "group1": None,
                "error": "metric_parser match subprocess returned malformed output"}
    if payload.get("error"):
        return {"matched": False, "group1": None, "error": payload["error"]}
    if not payload.get("matched"):
        err = None
        if full_len > METRIC_STDOUT_SEARCH_CAP:
            err = (
                f"metric_parser found no match in stdout truncated to first "
                f"{METRIC_STDOUT_SEARCH_CAP} characters (stdout was {full_len}); "
                "print the metric earlier in the output or trim it"
            )
        return {"matched": False, "group1": None, "error": err}
    return {"matched": True, "group1": payload.get("group1"), "error": None}


def _audit_path_matches_frozen(file_path: str, frozen: str) -> bool:
    """Component-boundary frozen-path match for the policy audit (R2-O4):
    equality, or containment under a frozen directory entry. The previous
    suffix match (`endswith("/" + fp)`) gave both false positives
    (`vendor/src/foo.py` vs frozen `src/foo.py`) and false negatives
    (frozen `src/` never matched `src/foo.py`).

    Replicates `sessions-enforce._path_matches_frozen` semantics. Replicated,
    NOT imported: the hook file has a hyphenated name and executes
    `json.load(sys.stdin)` at module level, so importing it would hang.
    Git-log paths are already repo-relative; normalisation here only smooths
    user-authored frozen entries (backslashes, leading `./` or `/`).
    """
    def _norm(p: str) -> str:
        s = (p or "").replace("\\", "/").strip()
        while s.startswith("./"):
            s = s[2:]
        return s.lstrip("/").rstrip("/")

    t = _norm(file_path)
    f = _norm(frozen)
    if not t or not f:
        return False
    return t == f or t.startswith(f + "/")


class OptimizeCompletionMixin:
    """Optimize-protocol + completion-dispatch funcs composed into ProtocolEngine."""

    _METRIC_CMD_ALLOWED_PREFIXES = (
        "python", "python3",
        "node", "deno",
        "bash", "sh",
        "make",
        "npm", "yarn", "pnpm",
        "cargo", "go",
        "pytest", "jest", "rspec",
    )
    _METRIC_ENV_ALLOWLIST = (
        "PATH", "HOME", "USER", "LANG",
        "VIRTUAL_ENV",
        "CARGO_HOME", "CARGO_MANIFEST_DIR",
        "NODE_PATH",
        "PYTHONPATH", "PYTHONDONTWRITEBYTECODE",
        "GOPATH", "RUSTUP_HOME",
    )
    _CREDENTIAL_KEY_PATTERNS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")

    _TSV_HEADER = (
        "iteration\ttimestamp\tcommit_sha\tmetric_value\trun_count\t"
        "aggregator\twall_clock_s\tstatus\thypothesis\n"
    )

    def _read_optimize_state(self) -> Dict:
        """Read .claude/state/optimize-state.json. Returns {} if absent / unreadable.
        Caller decides whether absence is fatal."""
        state_path = self.state_dir / "optimize-state.json"
        if not state_path.exists():
            return {}
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except (IOError, OSError, json.JSONDecodeError):
            return {}

    def _build_metric_env(self, user_env_pass: List[str]) -> Dict[str, str]:
        """Build a filtered subprocess env dict.

        Start with `os.environ`, drop any key whose UPPER form contains a
        credential pattern (KEY/TOKEN/SECRET/PASSWORD/CREDENTIAL), then keep
        only keys in the default allowlist plus user-supplied `env_pass`.
        """
        allowlist = set(self._METRIC_ENV_ALLOWLIST) | set(user_env_pass or [])
        env: Dict[str, str] = {}
        for key, value in os.environ.items():
            upper_key = key.upper()
            if any(pattern in upper_key for pattern in self._CREDENTIAL_KEY_PATTERNS):
                continue
            if key in allowlist:
                env[key] = value
        return env

    def _aggregate_metric(self, values: List[float], aggregator: str) -> Optional[float]:
        """Collapse N runs to a comparable scalar via configured aggregator."""
        if not values:
            return None
        if aggregator == "median":
            sorted_vals = sorted(values)
            n = len(sorted_vals)
            if n % 2 == 1:
                return sorted_vals[n // 2]
            return (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2
        if aggregator == "mean":
            return sum(values) / len(values)
        if aggregator == "min":
            return min(values)
        if aggregator == "max":
            return max(values)
        return None

    @staticmethod
    def _filter_engine_owned_dirty_lines(porcelain_stdout: str, task_name: str) -> List[str]:
        """Filter `git status --porcelain` output, dropping ONLY the specific
        engine-written files under the active task's namespace:

          * `team-management/tasks/<task>.md`              (file-task .md)
          * `team-management/tasks/<task>/README.md`       (dir-task .md — the
            engine resolves dir-task files via `tasks_dir / task / "README.md"`
            in 10+ call sites, e.g. `_set_optimize_field` line 4920 which is
            the writer invoked by `_func_update_best_commit` after each
            improving iteration)
          * `team-management/tasks/<task>/results.tsv`     (TSV writer)
          * `team-management/tasks/<task>/.results.tsv.bak` (backup rotation)
          * `team-management/tasks/<task>/results.tsv.run-<N>` (restart archives)
          * `team-management/tasks/<task>/resume-*.txt`    (resume bookkeeping)

        User-authored files inside the task directory (e.g. `metric.py`,
        fixtures) are NOT filtered — those edits must still block the gate
        so the recorded commit_sha matches the measured tree.

        Returns the porcelain lines that represent genuine user-facing
        changes — empty list means the working tree is effectively clean
        from the dirty-tree gate's perspective. Path-extraction handles
        both standard `XY <path>` and rename `R  <old> -> <new>` formats,
        plus git's quoted-path encoding for paths with special chars.
        Assumes porcelain v1 format (the call site invokes
        `git status --porcelain` without --porcelain=v2).
        """
        task_md_file_mode = f"team-management/tasks/{task_name}.md"
        task_dir = f"team-management/tasks/{task_name}/"
        task_dir_no_slash = f"team-management/tasks/{task_name}"

        def _is_engine_owned(path: str) -> bool:
            if path == task_md_file_mode:
                return True
            # Bare task-dir entry: when git's --untracked-files setting is
            # `normal` (or repo config overrides our --untracked-files=all),
            # an entirely untracked task directory collapses to a single
            # `?? team-management/tasks/<task>/` line. Treat the directory
            # entry itself as engine-owned — its contents are filtered
            # individually when expanded.
            if path == task_dir_no_slash or path == task_dir:
                return True
            if not path.startswith(task_dir):
                return False
            rel = path[len(task_dir):]
            if rel == "README.md":
                return True  # dir-task .md (canonical convention across engine)
            if rel == "results.tsv" or rel == ".results.tsv.bak":
                return True
            if rel.startswith("results.tsv.run-"):
                return True  # restart archive (results.tsv.run-1, run-2, …)
            if rel.startswith("resume-") and rel.endswith(".txt"):
                return True  # resume-stdout-tail.txt, resume-blocked.txt
            return False

        def _normalize(p: str) -> str:
            p = p.strip()
            if len(p) >= 2 and p.startswith('"') and p.endswith('"'):
                p = p[1:-1]
            return p

        relevant: List[str] = []
        for line in porcelain_stdout.splitlines():
            if len(line) < 4:
                continue
            # Porcelain format: 2 status chars + space + path. Renames/copies
            # carry `<old> -> <new>`; for those, BOTH paths must be
            # engine-owned for the line to be filtered. Otherwise a user file
            # renamed INTO an engine-owned name (e.g. `git mv src/foo.py
            # team-management/tasks/<task>/results.tsv`) would silently bypass
            # the gate even though the user-side change (deletion from src/)
            # is real.
            path = line[3:]
            if " -> " in path:
                old_path, new_path = path.split(" -> ", 1)
                old_path = _normalize(old_path)
                new_path = _normalize(new_path)
                if _is_engine_owned(old_path) and _is_engine_owned(new_path):
                    continue
                relevant.append(line)
                continue
            path = _normalize(path)
            if _is_engine_owned(path):
                continue
            relevant.append(line)
        return relevant

    def _func_log_experiment_result(self, args: Dict = None) -> Dict:
        """Engine-owned experimentation row writer. Always invokes
        `_func_run_metric()` to measure on HEAD — LLM-passed metric values
        are NOT trusted (the engine is the single source of truth for the
        leaderboard). Float-validation at write site is a defensive backstop:
        any NaN/inf escaping run_metric is recorded as a `crash` row.
        Tabs/newlines in `hypothesis` are stripped to preserve TSV layout.
        Backup rotation: every 100 data rows the TSV is copied to
        .results.tsv.bak.

        Workflow:
            1. control-call short-circuit (approve_next_batch / exit_loop)
            2. dirty-tree pre-check (git status --porcelain) — refuse with
               actionable error if working tree has uncommitted changes
            3. invoke self._func_run_metric() — propagate failure verbatim
            4. assemble TSV row from run_metric result + LLM-supplied hypothesis
            5. resolve commit_sha (explicit arg > git rev-parse > "-")
            6. append row, rotate backup if needed

        Args:
            hypothesis: free-text single-line label (recorded in TSV).
            commit_sha: optional 7-char short SHA. Falls back to
                        `git rev-parse --short HEAD` when omitted, empty,
                        or "-". Writes "-" only when git is also
                        unavailable (non-repo / timeout / OSError).
            iteration_override: optional, when set overrides the loop_iteration
                                read from current_task.json (used by terminator
                                summary rows: iteration=-1).
            status: optional, defaults to "ok". Internal use only — callers
                    should not pass this; the func sets "crash" when
                    defensive float-validation rejects metric_value.
        """
        args = args or {}
        # Control-call short-circuit: when the user advances with
        # approve_next_batch=True or exit_loop=True alone, the engine re-runs
        # this step's post_funcs without a new iteration in between. Logging
        # again would write a spurious duplicate row.
        if args.get("approve_next_batch") or args.get("exit_loop"):
            return {"func": "log_experiment_result", "success": True,
                    "skipped_reason": "control-call (approve_next_batch / exit_loop)"}
        task_state = get_task_state()
        task_name = task_state.get("task")
        if not task_name:
            return {"func": "log_experiment_result", "success": False,
                    "error": "no active task — cannot locate results.tsv"}

        protocol_info = task_state.get("protocol") or {}
        iteration = args.get("iteration_override")
        if iteration is None:
            iteration = protocol_info.get("loop_iteration", 0)

        # Internal-summary path (status="summary"): the terminator row written
        # by _func_check_termination carries metadata, not a measurement.
        # Skip both the dirty-tree gate (the run is over) and the run_metric
        # call (there is no metric to measure). Use the values from args
        # verbatim — only this internal callsite is allowed to set status.
        is_summary = args.get("status") == "summary"
        if is_summary:
            try:
                metric_float = float(args.get("metric_value", 0.0) or 0.0)
            except (TypeError, ValueError):
                metric_float = 0.0
            try:
                run_count = int(args.get("run_count", 0) or 0)
            except (TypeError, ValueError):
                run_count = 0
            try:
                wall_clock_s = float(args.get("wall_clock_s", 0.0) or 0.0)
            except (TypeError, ValueError):
                wall_clock_s = 0.0
            aggregator = str(args.get("aggregator") or "-")
            # Resolve commit_sha for summary path (no run_metric to interfere
            # with HEAD ordering, so timing relative to subprocess doesn't
            # matter here — kept symmetric with the experimentation path).
            commit_sha = str(args.get("commit_sha") or "").strip()
            if not commit_sha or commit_sha == "-":
                try:
                    proc = subprocess.run(
                        ["git", "rev-parse", "--short", "HEAD"],
                        capture_output=True, text=True, timeout=30,
                        cwd=str(self.project_root), check=False,
                    )
                    if proc.returncode == 0:
                        commit_sha = proc.stdout.strip() or "-"
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if not commit_sha:
                commit_sha = "-"
        else:
            # Dirty-tree pre-check: the leaderboard is only meaningful when
            # results.tsv rows are tied to actual commits. If the working tree
            # has uncommitted changes that affect the measured behaviour, the
            # metric we'd measure does not match the commit_sha we'd record.
            # Refuse with a suggested commit message.
            #
            # Engine-owned task artifacts are excluded from the check:
            #   - team-management/tasks/<task>.md (file-task; update_best_commit
            #     writes optimize.best_commit/best_metric frontmatter on each
            #     improving iteration)
            #   - team-management/tasks/<task>/... (directory-task contents,
            #     including results.tsv written by this very func and its
            #     .results.tsv.bak rotations)
            # Otherwise iteration N+1 would always fail the gate after an
            # improving iteration N — the user would be stuck in a loop where
            # every advance after the first improvement is refused.
            #
            # (The check lives here, not in _func_run_metric, because
            # _func_validate_metric_script legitimately runs run_metric while
            # the metric script itself is uncommitted during the metric-script
            # step.)
            try:
                # --untracked-files=all forces git to expand untracked
                # directories into per-file entries. Without it, a fresh
                # task dir shows up as a single `?? team-management/tasks/<task>/`
                # line and the per-file allowlist below cannot match. Defense
                # in depth: the helper also recognises the bare task-dir entry
                # in case --untracked-files=all is overridden by repo config.
                #
                # -c core.quotePath=false disables git's C-quoting of paths
                # containing non-ASCII characters (default `core.quotePath=on`
                # would emit `\303\251` for `é` etc., breaking the per-file
                # allowlist match for tasks named with Unicode characters).
                status_proc = subprocess.run(
                    ["git", "-c", "core.quotePath=false",
                     "status", "--porcelain", "--untracked-files=all"],
                    capture_output=True, text=True, timeout=30,
                    cwd=str(self.project_root), check=False,
                )
                if status_proc.returncode == 0 and status_proc.stdout.strip():
                    relevant_dirty = self._filter_engine_owned_dirty_lines(
                        status_proc.stdout, task_name
                    )
                    if relevant_dirty:
                        hypothesis_hint = str(args.get("hypothesis") or "<your-hypothesis-label>").strip() or "<your-hypothesis-label>"
                        return {"func": "log_experiment_result", "success": False,
                                "error": (
                                    "working tree has uncommitted changes — commit before advancing.\n"
                                    "Suggested:\n"
                                    "  git add <files>\n"
                                    f"  git commit -m 'iter{iteration}: {hypothesis_hint}'\n"
                                    "Then re-call protocol_advance with the same hypothesis."
                                ),
                                "dirty_files": "\n".join(relevant_dirty),
                                "stage": "dirty_tree_check"}
            except (OSError, subprocess.TimeoutExpired):
                pass  # non-repo / git unavailable — skip check defensively

            # Resolve commit_sha BEFORE run_metric. If the metric script has
            # any side effect on HEAD (checkout, commit, etc.), capturing
            # the SHA after run_metric would record the post-run HEAD, not
            # the revision that was actually measured. Belt-and-braces with
            # the dirty-tree check above (which catches gross uncommitted
            # changes but cannot detect a clean `git checkout <other-sha>`).
            # Pre-resolved SHA wins over the post-run fallback below.
            commit_sha = str(args.get("commit_sha") or "").strip()
            if not commit_sha or commit_sha == "-":
                try:
                    proc = subprocess.run(
                        ["git", "rev-parse", "--short", "HEAD"],
                        capture_output=True, text=True, timeout=30,
                        cwd=str(self.project_root), check=False,
                    )
                    if proc.returncode == 0:
                        commit_sha = proc.stdout.strip() or "-"
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if not commit_sha:
                commit_sha = "-"

            # Engine measures the metric on HEAD. LLM-passed metric_value/
            # run_count/aggregator/wall_clock_s are not consulted — the engine
            # is the single source of truth for the leaderboard.
            metric_result = self._func_run_metric()
            if not metric_result.get("success"):
                return {"func": "log_experiment_result", "success": False,
                        "error": metric_result.get("error", "run_metric failed"),
                        "raw_outputs": metric_result.get("raw_outputs", []),
                        "stage": "run_metric"}

            metric_float = metric_result["metric_value"]
            run_count = metric_result["run_count"]
            wall_clock_s = metric_result["wall_clock_s"]
            aggregator = metric_result["aggregator"]

        # Sanitise hypothesis: strip tabs/newlines (would corrupt TSV layout)
        hypothesis = str(args.get("hypothesis", ""))
        for ch in ("\t", "\n", "\r"):
            hypothesis = hypothesis.replace(ch, " ")
        hypothesis = hypothesis.strip()

        # Defensive float-validation: run_metric should never produce
        # NaN/inf, but defence in depth.
        metric_str = "NaN"
        status = args.get("status", "ok")
        try:
            metric_float = float(metric_float)
            if math.isnan(metric_float) or math.isinf(metric_float):
                status = "crash"
            else:
                # repr() yields the shortest round-trippable representation
                # (repr(42.5) -> '42.5', repr(4.0) -> '4.0'). Use it for
                # both integer and fractional floats.
                metric_str = repr(metric_float)
        except (TypeError, ValueError):
            status = "crash"

        # commit_sha was resolved above per-branch (summary or experimentation).
        # Mirrors _func_update_best_commit:4406-4421 in the experimentation
        # path — both funcs run in the same post_funcs chain and yield the
        # same SHA. The experimentation path captures BEFORE _func_run_metric
        # to preserve the "row metric matches recorded SHA" invariant even if
        # the metric script has HEAD side effects.

        cols = [
            str(iteration),
            datetime.now(timezone.utc).isoformat(),
            commit_sha,
            metric_str,
            str(run_count),
            str(aggregator),
            f"{wall_clock_s:.3f}",
            status,
            hypothesis,
        ]

        task_dir = self.project_root / "team-management" / "tasks" / task_name
        try:
            task_dir.mkdir(parents=True, exist_ok=True)
        except (IOError, OSError) as e:
            return {"func": "log_experiment_result", "success": False,
                    "error": f"failed to create task dir: {e}"}
        tsv_path = task_dir / "results.tsv"
        write_header = not tsv_path.exists()

        try:
            with open(tsv_path, "a", encoding="utf-8") as f:
                if write_header:
                    f.write(self._TSV_HEADER)
                f.write("\t".join(cols) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except (IOError, OSError) as e:
            return {"func": "log_experiment_result", "success": False,
                    "error": f"TSV write failed: {e}"}

        # Backup rotation: every 100 data rows
        data_rows = 0
        try:
            with open(tsv_path, "r", encoding="utf-8") as f:
                line_count = sum(1 for _ in f)
            data_rows = max(0, line_count - 1)  # subtract header
            if data_rows > 0 and data_rows % 100 == 0:
                backup_path = task_dir / ".results.tsv.bak"
                shutil.copy2(str(tsv_path), str(backup_path))
        except (IOError, OSError):
            pass

        return {
            "func": "log_experiment_result",
            "success": True,
            "row_count": data_rows,
            "status": status,
            "metric_value_written": metric_str,
            "file": str(tsv_path.relative_to(self.project_root)),
        }

    def _func_run_metric(self, args: Dict = None) -> Dict:
        """Run the configured metric command N times, parse each stdout via
        the metric_parser regex (group 1 → float), aggregate via the
        configured aggregator, return the scalar plus total wall-clock.

        Reads from .claude/state/optimize-state.json:
            metric_command: str (validated via _validate_run_command)
            metric_parser:  str (regex; first capture group → float)
            runs_per_iteration: int (default 1)
            aggregator: "median" / "mean" / "min" / "max"
            env_pass: list[str] (extends env allowlist)
        """
        state = self._read_optimize_state()
        if not state:
            return {"func": "run_metric", "success": False,
                    "error": "optimize-state.json missing or unreadable — run setup first"}

        metric_command = state.get("metric_command")
        metric_parser = state.get("metric_parser")
        try:
            runs_per_iteration = int(state.get("runs_per_iteration", 1) or 1)
        except (TypeError, ValueError):
            runs_per_iteration = 1
        aggregator = state.get("aggregator", "median") or "median"
        user_env_pass = list(state.get("env_pass") or [])

        if not metric_command:
            return {"func": "run_metric", "success": False,
                    "error": "metric_command not set in optimize-state.json"}
        if not metric_parser:
            return {"func": "run_metric", "success": False,
                    "error": "metric_parser not set in optimize-state.json"}

        validation = self._validate_run_command(metric_command, self._METRIC_CMD_ALLOWED_PREFIXES)
        if not validation["success"]:
            return {"func": "run_metric", "success": False, "error": validation["error"]}
        argv = validation["argv"]

        # Upfront syntax validation only — the actual match runs in a killable
        # child process (see _bounded_regex_search).
        try:
            re.compile(metric_parser, re.MULTILINE)
        except re.error as e:
            return {"func": "run_metric", "success": False,
                    "error": f"metric_parser regex invalid: {e}"}

        filtered_env = self._build_metric_env(user_env_pass)

        raw_outputs: List[Dict] = []
        metric_values: List[float] = []
        total_wall_clock = 0.0

        for run_n in range(runs_per_iteration):
            try:
                t0 = time.monotonic()
                result = subprocess.run(
                    argv,
                    env=filtered_env,
                    shell=False,
                    capture_output=True,
                    text=True,
                    timeout=600,
                    cwd=str(self.project_root),
                    stdin=subprocess.DEVNULL,
                )
                wall = time.monotonic() - t0
                total_wall_clock += wall
            except subprocess.TimeoutExpired:
                return {"func": "run_metric", "success": False,
                        "error": f"metric command timed out (600s) on run {run_n+1}/{runs_per_iteration}",
                        "raw_outputs": raw_outputs}
            except (OSError, FileNotFoundError) as e:
                return {"func": "run_metric", "success": False,
                        "error": f"metric command failed to execute: {e}",
                        "raw_outputs": raw_outputs}

            raw_outputs.append({
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.returncode,
            })

            if result.returncode != 0:
                return {"func": "run_metric", "success": False,
                        "error": f"metric command exit {result.returncode} on run {run_n+1}/{runs_per_iteration}",
                        "raw_outputs": raw_outputs}

            search = _bounded_regex_search(metric_parser, result.stdout or "")
            if search.get("error"):
                return {"func": "run_metric", "success": False,
                        "error": f"{search['error']} (run {run_n+1}/{runs_per_iteration})",
                        "raw_outputs": raw_outputs}
            if not search.get("matched"):
                return {"func": "run_metric", "success": False,
                        "error": f"metric_parser failed to parse stdout on run {run_n+1}/{runs_per_iteration}",
                        "stdout": result.stdout,
                        "raw_outputs": raw_outputs}
            if search.get("group1") is None:
                return {"func": "run_metric", "success": False,
                        "error": ("failed to extract float from parser match: group(1) "
                                  "is missing or did not participate in the match — "
                                  "metric_parser needs a non-optional capture group "
                                  "around the number"),
                        "raw_outputs": raw_outputs}
            try:
                value = float(search["group1"])
            except ValueError as e:
                return {"func": "run_metric", "success": False,
                        "error": f"failed to extract float from parser match: {e}",
                        "raw_outputs": raw_outputs}
            metric_values.append(value)

        aggregated = self._aggregate_metric(metric_values, aggregator)
        if aggregated is None:
            return {"func": "run_metric", "success": False,
                    "error": f"unknown aggregator {aggregator!r} (allowed: median, mean, min, max)"}

        return {
            "func": "run_metric",
            "success": True,
            "metric_value": aggregated,
            "run_count": runs_per_iteration,
            "wall_clock_s": total_wall_clock,
            "aggregator": aggregator,
            "raw_outputs": raw_outputs,
            "values": metric_values,
        }

    def _func_validate_metric_script(self, args: Dict = None) -> Dict:
        """Pre-flight before experimentation: run the metric command twice and
        assert (a) both runs succeed; (b) the two values are within
        `stability_threshold_pct` (default 5%) of each other. On success the
        existing optimize-state.json is left as-is (the script is already
        recorded; this is the validation pass). On failure return an actionable
        error directing the user to revise their script.
        """
        state = self._read_optimize_state()
        if not state:
            return {"func": "validate_metric_script", "success": False,
                    "error": "optimize-state.json missing — run setup first"}
        try:
            threshold_pct = float(state.get("stability_threshold_pct", 5.0) or 5.0)
        except (TypeError, ValueError):
            threshold_pct = 5.0

        first = self._func_run_metric()
        if not first.get("success"):
            return {"func": "validate_metric_script", "success": False,
                    "error": f"first validation run failed: {first.get('error')}",
                    "first_run": first}
        v1 = first["metric_value"]

        second = self._func_run_metric()
        if not second.get("success"):
            return {"func": "validate_metric_script", "success": False,
                    "error": f"second validation run failed: {second.get('error')}",
                    "second_run": second}
        v2 = second["metric_value"]

        # Symmetric at zero (R2-O3): max(|v1|,|v2|) as denominator — both zero
        # → 0% (stable), exactly one zero → 100% in either order. The previous
        # abs(v1) denominator yielded inf for v1==0 but 100% for v2==0.
        denom = max(abs(v1), abs(v2))
        delta_pct = 0.0 if denom == 0 else abs(v2 - v1) / denom * 100.0

        if delta_pct > threshold_pct:
            zero_hint = ""
            if v1 == 0 or v2 == 0:
                zero_hint = (
                    " Note: one validation run returned 0 — if the metric "
                    "legitimately starts at zero (cold cache, warm-up effects), "
                    "add a warm-up run to the metric script or raise "
                    "stability_threshold_pct."
                )
            return {
                "func": "validate_metric_script",
                "success": False,
                "error": (
                    f"metric instability: run 1 = {v1}, run 2 = {v2}, "
                    f"delta = {delta_pct:.2f}%, stability_threshold_pct = {threshold_pct}%. "
                    f"Revise the metric script to be more deterministic, or raise "
                    f"stability_threshold_pct in optimize-state.json." + zero_hint
                ),
                "first": v1,
                "second": v2,
                "delta_pct": delta_pct,
                "threshold_pct": threshold_pct,
            }

        return {
            "func": "validate_metric_script",
            "success": True,
            "first": v1,
            "second": v2,
            "delta_pct": delta_pct,
            "threshold_pct": threshold_pct,
            "message": f"Metric script stable: {v1} ≈ {v2} (Δ {delta_pct:.2f}% ≤ {threshold_pct}%)",
        }

    def _func_capture_metric_baseline(self, args: Dict = None) -> Dict:
        """Run the metric once on the current HEAD; record baseline_metric +
        baseline_wall_clock_s in optimize-state.json and `optimize.baseline_commit`
        in task frontmatter. Used by `_func_check_cost_estimate` for the
        cost projection (real timing, not a user estimate).
        """
        run = self._func_run_metric()
        if not run.get("success"):
            return {"func": "capture_metric_baseline", "success": False,
                    "error": f"baseline run failed: {run.get('error')}",
                    "run_result": run}

        baseline_commit = "-"
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=30,
                cwd=str(self.project_root),
                check=False,
            )
            if proc.returncode == 0:
                baseline_commit = proc.stdout.strip() or "-"
        except (OSError, subprocess.TimeoutExpired):
            pass

        # Persist into optimize-state.json
        state = self._read_optimize_state() or {}
        state["baseline_metric"] = run["metric_value"]
        state["baseline_wall_clock_s"] = run["wall_clock_s"]
        state["baseline_commit"] = baseline_commit
        try:
            write_optimize_state(state)
        except (IOError, OSError) as e:
            return {"func": "capture_metric_baseline", "success": False,
                    "error": f"failed to persist baseline to optimize-state.json: {e}"}

        # Persist baseline_commit into task frontmatter as a flat key
        task_state = get_task_state()
        task_name = task_state.get("task")
        if task_name:
            self._set_optimize_field(task_name, "baseline_commit", baseline_commit)

        return {
            "func": "capture_metric_baseline",
            "success": True,
            "baseline_metric": run["metric_value"],
            "baseline_wall_clock_s": run["wall_clock_s"],
            "baseline_commit": baseline_commit,
        }

    _OPTIMIZE_SETUP_KEYS = frozenset({
        "metric_command", "metric_parser", "metric_direction", "metric_monotonic",
        "frozen_paths", "env_pass", "runs_per_iteration", "aggregator",
        "stability_threshold_pct", "max_iterations", "max_duration",
        "target_metric", "regression_halt_n", "batch_size",
    })

    @staticmethod
    def _parse_duration_to_seconds(value) -> Optional[float]:
        """Normalise a max_duration value to seconds. Accepts None, numeric,
        or shorthand strings (``"<N>h"``, ``"<N>m"``, ``"<N>s"``, plain
        ``"<N>"``). Returns None for None input. Raises ValueError on
        anything else (the caller turns this into an actionable setup error).

        Why this lives at the setup boundary rather than inside
        ``_func_check_termination``: T2's check_termination does ``float(
        max_duration)`` and silently ``pass``es on ValueError (line 4135-4136),
        so a non-numeric string default like ``"8h"`` would silently disable
        the wall-clock cap. Normalising here means the on-disk
        ``optimize-state.json`` always stores a numeric seconds value, and
        check_termination's ``float(...)`` works correctly without changes.
        """
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError(f"max_duration must be a duration, not bool: {value!r}")
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value).strip().lower()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            pass
        suffix_map = {"h": 3600.0, "m": 60.0, "s": 1.0}
        if s and s[-1] in suffix_map:
            try:
                return float(s[:-1]) * suffix_map[s[-1]]
            except ValueError:
                pass
        raise ValueError(
            f"invalid max_duration {value!r}: expected null, a number of seconds, "
            f"or shorthand like '8h' / '30m' / '90s'"
        )

    def _validate_optimize_setup_args(self, args: Dict) -> tuple:
        """Validate optimize-setup args. Returns (state_dict, None) on success
        or (None, error_dict) on failure. Shared between
        ``_func_validate_optimize_setup`` (early gate, no write) and
        ``_func_write_optimize_setup`` (late write, defensive re-validate).
        """
        def _err(msg: str) -> tuple:
            return None, {"success": False, "error": msg}

        required = ("metric_command", "metric_parser", "metric_direction", "metric_monotonic")
        missing = [k for k in required if k not in args]
        if missing:
            return _err(f"missing required arg(s): {', '.join(missing)}")
        direction = args["metric_direction"]
        if direction not in ("min", "max"):
            return _err(f"metric_direction must be 'min' or 'max', got {direction!r}")
        aggregator = args.get("aggregator", "median")
        if aggregator not in ("median", "mean", "min", "max"):
            return _err(f"aggregator must be one of median/mean/min/max, got {aggregator!r}")
        # metric_monotonic must be a strict bool — accepting truthy strings
        # like "false" would defeat the v1 monotonic gate since
        # bool("false") == True.
        if not isinstance(args["metric_monotonic"], bool):
            return _err(
                f"metric_monotonic must be a boolean (True/False), got "
                f"{type(args['metric_monotonic']).__name__}: {args['metric_monotonic']!r}"
            )
        # The optimize protocol's v1 contract supports only monotonic metrics
        # (see optimize-setup.md §1.3). Defence-in-depth against an LLM that
        # misses the sub-protocol's "halt" instruction.
        if not args["metric_monotonic"]:
            return _err(
                "metric_monotonic=false is not supported by optimize v1. "
                "Either narrow the metric to a regime where the direction "
                "always holds (capture that as a constraint and re-issue "
                "setup with metric_monotonic=true), or abort and use the "
                "research protocol instead."
            )
        # frozen_paths and env_pass must be lists. A stringified value like
        # "src/foo.py" would silently coerce via list(...) to a char array,
        # disabling frozen-path enforcement for the real path.
        for list_field in ("frozen_paths", "env_pass"):
            if list_field in args and args[list_field] is not None:
                if not isinstance(args[list_field], list):
                    return _err(
                        f"{list_field} must be a list of strings, got "
                        f"{type(args[list_field]).__name__}: {args[list_field]!r}"
                    )
        try:
            # `or default` fallbacks are deliberately limited to fields where
            # zero would be meaningless (runs_per_iteration=0, batch_size=0
            # are nonsensical). `stability_threshold_pct=0` IS meaningful
            # (strict-determinism gate: validator must produce identical
            # values across two runs), so use a None-only fallback.
            runs_per_iteration = int(args.get("runs_per_iteration", 1) or 1)
            raw_stab = args.get("stability_threshold_pct", 5.0)
            stability_threshold_pct = 5.0 if raw_stab is None else float(raw_stab)
            batch_size = int(args.get("batch_size", 3) or 3)
            # Coerce termination caps strictly so a stringified value (e.g.
            # an LLM passing "fifty" or "unbounded") doesn't silently bypass
            # the safety contract via check_termination's silent except: pass.
            raw_max_iter = args.get("max_iterations", 50)
            max_iterations = None if raw_max_iter is None else int(raw_max_iter)
            raw_reg_halt = args.get("regression_halt_n", 5)
            regression_halt_n = None if raw_reg_halt is None else int(raw_reg_halt)
            raw_target = args.get("target_metric", None)
            target_metric = None if raw_target is None else float(raw_target)
        except (TypeError, ValueError) as e:
            return _err(f"numeric arg coercion failed: {e}")
        try:
            max_duration_s = self._parse_duration_to_seconds(args.get("max_duration", "8h"))
        except ValueError as e:
            return _err(str(e))
        state = {
            "metric_command": args["metric_command"],
            "metric_parser": args["metric_parser"],
            "metric_direction": direction,
            "metric_monotonic": True,
            "frozen_paths": list(args.get("frozen_paths") or []),
            "env_pass": list(args.get("env_pass") or []),
            "runs_per_iteration": runs_per_iteration,
            "aggregator": aggregator,
            "stability_threshold_pct": stability_threshold_pct,
            "max_iterations": max_iterations,
            "max_duration": max_duration_s,
            "target_metric": target_metric,
            "regression_halt_n": regression_halt_n,
            "batch_size": batch_size,
        }
        return state, None

    @staticmethod
    def _detect_optimize_key_typos(args: Dict) -> List[Dict]:
        """Surface args that fuzzy-match a recognised optimize key but are
        not exact matches. Used as the typo-guard advisory in the result
        dict — does not block the chain.
        """
        import difflib
        suspicious_keys: List[Dict] = []
        recognised = OptimizeCompletionMixin._OPTIMIZE_SETUP_KEYS
        for key in args:
            if key in recognised:
                continue
            close = difflib.get_close_matches(key, recognised, n=1, cutoff=0.7)
            if close:
                suspicious_keys.append({"got": key, "did_you_mean": close[0]})
        return suspicious_keys

    def _func_validate_optimize_setup(self, args: Dict = None) -> Dict:
        """Early-gate post_func of the optimize ``setup`` step. Validates
        user-provided settings WITHOUT writing to disk — so a validation
        failure aborts before ``git_setup_branch`` / ``create_task_file`` /
        ``create_issue_if_enabled`` create durable side effects.

        ``write_optimize_setup`` runs as the last post_func and writes the
        validated state. Both funcs share ``_validate_optimize_setup_args``
        for the validation logic; the write func re-validates defensively.
        """
        args = args or {}
        state, err = self._validate_optimize_setup_args(args)
        if err:
            return {"func": "validate_optimize_setup", **err}
        return {
            "func": "validate_optimize_setup", "success": True,
            "message": "optimize-setup args valid", "state_preview": state,
            "suspicious_keys": self._detect_optimize_key_typos(args),
        }

    def _func_write_optimize_setup(self, args: Dict = None) -> Dict:
        """Persist user-provided optimize settings to optimize-state.json.

        Used as the LAST post_func of the optimize ``setup`` step, after
        ``validate_optimize_setup`` has already gated on validation and
        ``git_setup_branch`` / ``create_task_file`` / etc. have created the
        task scaffolding. Re-validates defensively (never trusts upstream
        chain). Closes the T2 gap where ``setup`` had no func to persist
        user settings (the file is in ``PROTECTED_PATHS``, so direct edits
        are blocked).

        The result includes a ``suspicious_keys`` list flagging args that
        fuzzy-match a recognised optimize key but are not exact matches
        (typo guard — e.g. ``frozen_path`` instead of ``frozen_paths``).
        The write still succeeds; the LLM is expected to surface the
        warning to the user.
        """
        args = args or {}
        state, err = self._validate_optimize_setup_args(args)
        if err:
            return {"func": "write_optimize_setup", **err}
        try:
            write_optimize_state(state)
        except (IOError, OSError) as e:
            return {
                "func": "write_optimize_setup", "success": False,
                "error": f"failed to write optimize-state.json: {e}",
            }
        return {
            "func": "write_optimize_setup", "success": True,
            "message": "optimize-state.json written", "state": state,
            "suspicious_keys": self._detect_optimize_key_typos(args),
        }

    def _func_check_cost_estimate(self, args: Dict = None) -> Dict:
        """Post-func at the end of `setup`. Derives a wall-clock cost projection
        from the real baseline timing recorded by `capture_metric_baseline`.
        For unbounded mode (max_iterations is None AND max_duration is None),
        require an explicit typed risk-checklist confirmation:
            args["unbounded_acknowledged"] == "i-accept-unbounded-cost"
        """
        args = args or {}
        state = self._read_optimize_state()
        if not state:
            return {"func": "check_cost_estimate", "success": False,
                    "error": "optimize-state.json missing — run setup first"}

        try:
            baseline_wc = float(state.get("baseline_wall_clock_s", 0.0) or 0.0)
        except (TypeError, ValueError):
            baseline_wc = 0.0
        try:
            runs_per_iteration = int(state.get("runs_per_iteration", 1) or 1)
        except (TypeError, ValueError):
            runs_per_iteration = 1

        max_iterations = state.get("max_iterations")
        max_duration = state.get("max_duration")

        # Unbounded → typed-ack gate
        if max_iterations is None and max_duration is None:
            if args.get("unbounded_acknowledged") != "i-accept-unbounded-cost":
                return {
                    "func": "check_cost_estimate",
                    "success": False,
                    "error": (
                        "Unbounded mode (max_iterations and max_duration both null) "
                        "requires typed risk-checklist acknowledgement. Re-call with "
                        "args={'unbounded_acknowledged': 'i-accept-unbounded-cost'}. "
                        "An unbounded loop runs until external interruption — be sure "
                        "the metric script and budget allow it."
                    ),
                    "unbounded": True,
                }
            return {
                "func": "check_cost_estimate",
                "success": True,
                "unbounded": True,
                "acknowledged": True,
                "message": "Unbounded mode acknowledged. Loop terminates only on manual interrupt.",
            }

        # Bounded — compute projection
        projection = {
            "baseline_wall_clock_s": baseline_wc,
            "runs_per_iteration": runs_per_iteration,
            "max_iterations": max_iterations,
            "max_duration_s": max_duration,
        }
        if max_iterations is not None:
            try:
                max_iter = int(max_iterations)
                projection["projected_wall_clock_s"] = baseline_wc * runs_per_iteration * max_iter
            except (TypeError, ValueError):
                pass

        return {
            "func": "check_cost_estimate",
            "success": True,
            "unbounded": False,
            "projection": projection,
            "message": (
                f"Cost projection: baseline_wall_clock_s={baseline_wc:.3f}s × "
                f"runs_per_iteration={runs_per_iteration} × max_iterations="
                f"{max_iterations} = "
                f"{projection.get('projected_wall_clock_s', 'n/a')}s "
                f"(max_duration_s cap: {max_duration})"
            ),
        }

    def _read_results_tsv_rows(self, task_name: str) -> List[Dict]:
        """Read results.tsv and return parsed rows as dicts (excluding header).
        Empty list when TSV missing. Used by `check_termination` and the
        completion-step leaderboard.
        """
        if not task_name:
            return []
        tsv_path = self.project_root / "team-management" / "tasks" / task_name / "results.tsv"
        if not tsv_path.exists():
            return []
        try:
            text = tsv_path.read_text(encoding="utf-8")
        except (IOError, OSError):
            return []
        lines = text.strip().split("\n")
        if len(lines) < 2:
            return []
        header = lines[0].split("\t")
        rows: List[Dict] = []
        for line in lines[1:]:
            cols = line.split("\t")
            if len(cols) != len(header):
                continue
            row = dict(zip(header, cols))
            try:
                row["iteration"] = int(row.get("iteration", "0"))
            except (TypeError, ValueError):
                continue
            try:
                row["metric_value"] = float(row["metric_value"])
            except (TypeError, ValueError):
                row["metric_value"] = float("nan")
            rows.append(row)
        return rows

    def _func_check_termination(self, args: Dict = None) -> Dict:
        """Post-func after each experimentation iteration. Checks four conditions
        in order; returns the first match as the termination reason. Appends a
        summary row to results.tsv on terminate.

        Conditions (priority order):
            1. loop_iteration >= max_iterations           → reason="max_iterations"
            2. now - experimentation_started_at >= max_duration → reason="max_duration"
            3. last `regression_halt_n` ok-rows all worse than best → reason="regression_halt"
            4. target_metric set and best meets it        → reason="target_reached"
        """
        args = args or {}
        # Control-call short-circuit (see _func_log_experiment_result for
        # rationale): re-evaluating termination on an approve_next_batch /
        # exit_loop call would re-fire condition 2 (max_duration) using the
        # original experimentation_started_at, which can flip the verdict
        # between iterations N and N+1 spuriously.
        if args.get("approve_next_batch") or args.get("exit_loop"):
            return {"func": "check_termination", "success": True, "terminate": False,
                    "skipped_reason": "control-call (approve_next_batch / exit_loop)"}
        state = self._read_optimize_state()
        if not state:
            # No optimize state → cannot decide; default to no-terminate
            return {"func": "check_termination", "success": True, "terminate": False,
                    "reason": "no optimize-state.json (no-op)"}

        max_iterations = state.get("max_iterations")
        max_duration = state.get("max_duration")
        regression_halt_n = state.get("regression_halt_n")
        target_metric = state.get("target_metric")
        metric_direction = state.get("metric_direction", "min")

        task_state = get_task_state()
        task_name = task_state.get("task")
        protocol_info = task_state.get("protocol") or {}
        loop_iteration = int(protocol_info.get("loop_iteration", 0) or 0)
        started_at = protocol_info.get("experimentation_started_at")

        # Condition 1: max_iterations
        if max_iterations is not None:
            try:
                if loop_iteration >= int(max_iterations):
                    return self._terminate(
                        task_name, "max_iterations",
                        f"loop_iteration={loop_iteration} reached max_iterations={max_iterations}",
                    )
            except (TypeError, ValueError):
                pass

        # Condition 2: max_duration
        if max_duration is not None and started_at:
            try:
                start_dt = datetime.fromisoformat(started_at)
                elapsed_s = (datetime.now(timezone.utc) - start_dt).total_seconds()
                max_dur_s = float(max_duration)
                if elapsed_s >= max_dur_s:
                    return self._terminate(
                        task_name, "max_duration",
                        f"elapsed={elapsed_s:.1f}s >= max_duration={max_dur_s}s",
                    )
            except (TypeError, ValueError):
                pass

        # Read TSV rows for conditions 3 and 4
        rows = self._read_results_tsv_rows(task_name)
        ok_rows = [r for r in rows if r.get("status") == "ok" and r["iteration"] >= 0]
        if not ok_rows:
            return {"func": "check_termination", "success": True, "terminate": False,
                    "reason": "no ok rows yet"}

        # Compute best so far
        best = self._best_row(ok_rows, metric_direction)
        best_value = best["metric_value"]

        # Condition 3: regression_halt_n consecutive worsenings
        if regression_halt_n is not None:
            try:
                n = int(regression_halt_n)
                last_n = ok_rows[-n:]
                if len(last_n) >= n:
                    all_worse = all(self._is_worse(r["metric_value"], best_value, metric_direction)
                                    for r in last_n)
                    if all_worse:
                        return self._terminate(
                            task_name, "regression_halt",
                            f"last {n} ok rows all worse than best={best_value}",
                        )
            except (TypeError, ValueError):
                pass

        # Condition 4: target_metric reached
        if target_metric is not None:
            try:
                target = float(target_metric)
                if metric_direction == "min" and best_value <= target:
                    return self._terminate(
                        task_name, "target_reached",
                        f"best={best_value} <= target_metric={target}",
                    )
                if metric_direction == "max" and best_value >= target:
                    return self._terminate(
                        task_name, "target_reached",
                        f"best={best_value} >= target_metric={target}",
                    )
            except (TypeError, ValueError):
                pass

        return {
            "func": "check_termination",
            "success": True,
            "terminate": False,
            "loop_iteration": loop_iteration,
            "best_metric": best_value,
        }

    def _best_row(self, rows: List[Dict], direction: str) -> Dict:
        """Return the row with the best metric_value per direction."""
        if direction == "max":
            return max(rows, key=lambda r: r["metric_value"])
        return min(rows, key=lambda r: r["metric_value"])

    def _is_worse(self, value: float, best: float, direction: str) -> bool:
        """Return True iff `value` is strictly worse than `best` per direction."""
        if direction == "max":
            return value < best
        return value > best

    def _terminate(self, task_name: str, reason: str, detail: str) -> Dict:
        """Append the summary row to results.tsv and return the termination dict."""
        # Append summary row via _func_log_experiment_result
        summary_args = {
            "metric_value": 0.0,
            "commit_sha": "-",
            "run_count": 0,
            "aggregator": "-",
            "wall_clock_s": 0.0,
            "hypothesis": f"TERMINATE {reason}: {detail}",
            "status": "summary",
            "iteration_override": -1,
        }
        log_result = self._func_log_experiment_result(args=summary_args)
        return {
            "func": "check_termination",
            "success": True,
            "terminate": True,
            "reason": reason,
            "detail": detail,
            "summary_row": log_result,
            "message": f"[TERMINATE] {reason}: {detail}",
        }

    def _func_update_best_commit(self, args: Dict = None) -> Dict:
        """Post-func after each iteration. If the new metric improves on the
        recorded best (per metric_direction from optimize-state.json), rewrite
        `optimize.best_commit` and `optimize.best_metric` in task frontmatter.
        First iteration always sets the best. Worse-than-current → no-op.

        Args (all optional — falls back to engine state when omitted):
            metric_value: explicit override (else read from last results.tsv row)
            commit_sha: explicit override (else read from `git rev-parse HEAD`)

        The fallback path (no args) is the normal protocol-wired case, where
        `update_best_commit` runs as a post_func of the looping experimentation
        step after `log_experiment_result` has already appended the iteration's
        row. The explicit-args path is for tests and ad-hoc callers.
        """
        args = args or {}
        # Control-call short-circuit (see _func_log_experiment_result): on
        # an approve_next_batch / exit_loop call no new iteration occurred,
        # so re-running update_best_commit would re-process the previous
        # iteration's row.
        if args.get("approve_next_batch") or args.get("exit_loop"):
            return {"func": "update_best_commit", "success": True,
                    "skipped_reason": "control-call (approve_next_batch / exit_loop)"}
        task_state = get_task_state()
        task_name = task_state.get("task")

        # Resolve metric_value: explicit arg > last TSV row > error
        raw_value = args.get("metric_value")
        if raw_value is None and task_name:
            rows = self._read_results_tsv_rows(task_name)
            ok_rows = [r for r in rows if r.get("status") == "ok" and r["iteration"] >= 0]
            if ok_rows:
                raw_value = ok_rows[-1]["metric_value"]
        try:
            new_value = float(raw_value)
        except (TypeError, ValueError):
            return {"func": "update_best_commit", "success": False,
                    "error": "metric_value not provided and last results.tsv row missing/non-numeric"}
        if math.isnan(new_value) or math.isinf(new_value):
            return {"func": "update_best_commit", "success": True,
                    "updated": False, "reason": "metric is NaN or inf"}

        # Resolve commit_sha: explicit arg > git rev-parse HEAD > error
        new_commit = str(args.get("commit_sha") or "").strip()
        if not new_commit or new_commit == "-":
            try:
                proc = subprocess.run(
                    ["git", "rev-parse", "--short", "HEAD"],
                    capture_output=True, text=True, timeout=30,
                    cwd=str(self.project_root), check=False,
                )
                if proc.returncode == 0:
                    new_commit = proc.stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                pass
        if not new_commit or new_commit == "-":
            return {"func": "update_best_commit", "success": False,
                    "error": "commit_sha not provided and git rev-parse HEAD failed"}

        state = self._read_optimize_state()
        direction = state.get("metric_direction", "min") if state else "min"

        task_state = get_task_state()
        task_name = task_state.get("task")
        if not task_name:
            return {"func": "update_best_commit", "success": False,
                    "error": "no active task"}

        # Read previous best from frontmatter
        frontmatter = parse_task_frontmatter(task_name) or {}
        prev_best_metric_str = frontmatter.get("optimize.best_metric")
        prev_best_metric = None
        if prev_best_metric_str is not None:
            try:
                prev_best_metric = float(prev_best_metric_str)
            except (TypeError, ValueError):
                prev_best_metric = None

        improved = False
        if prev_best_metric is None:
            improved = True  # first iteration sets the best
        else:
            if direction == "max":
                improved = new_value > prev_best_metric
            else:
                improved = new_value < prev_best_metric

        if not improved:
            return {
                "func": "update_best_commit",
                "success": True,
                "updated": False,
                "previous_best_metric": prev_best_metric,
                "candidate": new_value,
                "direction": direction,
            }

        self._set_optimize_field(task_name, "best_commit", new_commit)
        self._set_optimize_field(task_name, "best_metric", repr(new_value))

        return {
            "func": "update_best_commit",
            "success": True,
            "updated": True,
            "previous_best_metric": prev_best_metric,
            "new_best_metric": new_value,
            "new_best_commit": new_commit,
            "direction": direction,
        }

    def _func_policy_compliance_audit(self, args: Dict = None) -> Dict:
        """Synthesis-step post-func. Best-effort heuristic git-history scan
        between `optimize.baseline_commit` and HEAD looking for signs of
        metric gaming. NEVER blocks advance — findings go into step output
        as `metric_gaming_flags` for the code-review prompt to surface.

        Heuristics:
            (a) commits modifying paths listed in optimize-state.json:frozen_paths
            (b) commits whose added diff lines contain the literal best_metric
                value (suggests hardcoded-constant gaming)
            (c) commits modifying results.tsv (engine should be the only writer)
        """
        flags: List[Dict] = []

        task_state = get_task_state()
        task_name = task_state.get("task")
        frontmatter = parse_task_frontmatter(task_name) or {}
        baseline_commit = frontmatter.get("optimize.baseline_commit")
        best_metric_str = frontmatter.get("optimize.best_metric")

        if not baseline_commit or baseline_commit == "-":
            return {
                "func": "policy_compliance_audit",
                "success": True,
                "skipped": "no optimize.baseline_commit in frontmatter — nothing to audit",
                "metric_gaming_flags": [],
            }

        state = self._read_optimize_state() or {}
        frozen_paths = state.get("frozen_paths", []) or []

        # Heuristic (a) + (c): list files changed since baseline
        try:
            proc = subprocess.run(
                ["git", "log", "--name-only", "--pretty=format:%H", f"{baseline_commit}..HEAD"],
                capture_output=True, text=True, timeout=60,
                cwd=str(self.project_root),
                check=False,
            )
            if proc.returncode == 0:
                # Parse: alternating blocks of [SHA, file1, file2, ..., blank, SHA, ...]
                current_sha = None
                for raw in (proc.stdout or "").split("\n"):
                    line = raw.strip()
                    if not line:
                        current_sha = None
                        continue
                    # `--pretty=format:%H` produces full 40-char SHAs; pin
                    # to exactly 40 hex chars so hex-named files (build
                    # artifacts, content-addressed storage) are not
                    # misclassified as commits (Claude round-1 review).
                    if re.fullmatch(r"[0-9a-fA-F]{40}", line):
                        current_sha = line[:7]
                        continue
                    # Otherwise treat as a file path
                    file_path = line
                    if file_path.endswith("results.tsv") or file_path.endswith(".results.tsv.bak"):
                        flags.append({
                            "kind": "results_tsv_edited",
                            "commit": current_sha or "-",
                            "file": file_path,
                            "detail": "results.tsv was edited inside a hypothesis commit — engine should be the only writer",
                        })
                    for fp in frozen_paths:
                        if _audit_path_matches_frozen(file_path, fp):
                            flags.append({
                                "kind": "frozen_path_modified",
                                "commit": current_sha or "-",
                                "file": file_path,
                                "frozen_pattern": fp,
                                "detail": "frozen path was edited — workflow guard violated",
                            })
        except (OSError, subprocess.TimeoutExpired) as e:
            return {
                "func": "policy_compliance_audit",
                "success": True,
                "skipped": f"git log failed: {e}",
                "metric_gaming_flags": flags,
            }

        # Heuristic (b): scan diffs for literal best_metric value
        if best_metric_str:
            try:
                proc = subprocess.run(
                    ["git", "log", "-p", f"{baseline_commit}..HEAD"],
                    capture_output=True, text=True, timeout=60,
                    cwd=str(self.project_root),
                    check=False,
                )
                if proc.returncode == 0 and proc.stdout:
                    # Look for added lines (start with +, but not +++) containing the metric value literal
                    needle = best_metric_str.strip()
                    for raw_line in proc.stdout.split("\n"):
                        if raw_line.startswith("+") and not raw_line.startswith("+++"):
                            if needle in raw_line:
                                flags.append({
                                    "kind": "metric_constant_hardcoded",
                                    "needle": needle,
                                    "line": raw_line[:200],
                                    "detail": "best metric value appears as a literal in added code — possible metric gaming",
                                })
                                # One hit is enough to flag; further hits add noise
                                break
            except (OSError, subprocess.TimeoutExpired):
                pass

        return {
            "func": "policy_compliance_audit",
            "success": True,
            "metric_gaming_flags": flags,
            "baseline_commit": baseline_commit,
            "message": (
                f"Policy audit found {len(flags)} flag(s) (informational, non-blocking). "
                f"Surfaced to code-review prompt."
            ),
        }

    def _func_batch_checkpoint(self, args: Dict = None) -> Dict:
        """B-only post-func used by the `optimize` (interactive) protocol.

        Modulo-gated batch checkpoint (T4 closes the T2 gap where this fired
        on every iteration regardless of ``batch_size``):

        - Mid-batch (``(loop_iteration + 1) % batch_size != 0``): no-op,
          returns ``success=True``, allows advance to the next iteration.
        - Batch boundary AND ``args["approve_next_batch"]`` is not True:
          switches DAIC mode to discussion, returns a batch summary
          (last ``batch_size`` ok rows + best-so-far) and ``success=False``
          to gate the advance.
        - User approves with ``args["approve_next_batch"] == True``: returns
          ``success=True`` immediately (the engine continues to the next
          iteration).

        With ``post_funcs_stop_on_failure: true`` on the step, returning
        ``success=False`` aborts the advance.
        """
        args = args or {}
        if args.get("approve_next_batch"):
            return {
                "func": "batch_checkpoint",
                "success": True,
                "approved": True,
                "message": "Batch approved — continuing experimentation.",
            }
        # exit_loop is also a valid gate-release at a batch boundary — the
        # advance_step engine reads exit_loop after post_funcs to decide
        # whether to terminate the loop, so batch_checkpoint must let the
        # advance succeed for exit_loop to take effect.
        if args.get("exit_loop"):
            return {
                "func": "batch_checkpoint",
                "success": True,
                "exit_loop": True,
                "message": "exit_loop signal — releasing checkpoint gate.",
            }

        # Short-circuit when the loop has already terminated this advance.
        # `check_termination` runs earlier in the post_funcs chain and writes
        # a summary row (status=summary, iteration=-1) to results.tsv on
        # terminate. Without this short-circuit, a terminate-at-batch-boundary
        # case (e.g. max_iterations=4 with batch_size=2) would force the user
        # to pass the workaround `args={"exit_loop": True, "approve_next_batch": True}`
        # — friction that contradicts the natural "loop-is-over" semantics.
        task_state_for_term = get_task_state()
        task_name_for_term = task_state_for_term.get("task")
        if task_name_for_term:
            recent_rows = self._read_results_tsv_rows(task_name_for_term)
            if recent_rows and recent_rows[-1].get("status") == "summary":
                return {
                    "func": "batch_checkpoint",
                    "success": True,
                    "terminated_skip": True,
                    "message": "Loop terminated — skipping batch checkpoint.",
                }

        state = self._read_optimize_state() or {}
        try:
            batch_size = int(state.get("batch_size", 5) or 5)
        except (TypeError, ValueError):
            batch_size = 5
        if batch_size < 1:
            batch_size = 1

        task_state = get_task_state()
        proto = task_state.get("protocol") or {}
        try:
            loop_iteration = int(proto.get("loop_iteration", 0) or 0)
        except (TypeError, ValueError):
            loop_iteration = 0

        # Mid-batch: no-op, allow advance to the next iteration.
        if (loop_iteration + 1) % batch_size != 0:
            return {
                "func": "batch_checkpoint",
                "success": True,
                "awaiting_approval": False,
                "loop_iteration": loop_iteration,
                "batch_size": batch_size,
                "message": (
                    f"Mid-batch (iter {loop_iteration}, batch_size {batch_size}) — "
                    f"continuing without checkpoint."
                ),
            }

        # Batch boundary: switch to discussion mode and present the summary.
        try:
            set_daic_mode("discussion")
        except Exception:
            pass

        task_name = task_state.get("task")
        rows = self._read_results_tsv_rows(task_name)
        ok_rows = [r for r in rows if r.get("status") == "ok" and r["iteration"] >= 0]
        last_batch = ok_rows[-batch_size:]
        direction = state.get("metric_direction", "min")
        best = self._best_row(ok_rows, direction) if ok_rows else None

        summary_lines = ["Batch checkpoint:"]
        summary_lines.append(f"  Batch size: {batch_size}; last {len(last_batch)} ok rows:")
        for r in last_batch:
            summary_lines.append(
                f"    iter {r['iteration']:>3} | metric {r['metric_value']} | "
                f"commit {r.get('commit_sha', '-')} | {r.get('hypothesis', '')[:60]}"
            )
        if best:
            summary_lines.append(
                f"  Best so far: iter {best['iteration']} | metric {best['metric_value']} | commit {best.get('commit_sha', '-')}"
            )
        summary_lines.append(
            "Pass args={'approve_next_batch': True} to protocol_advance to continue."
        )

        return {
            "func": "batch_checkpoint",
            "success": False,
            "awaiting_approval": True,
            "error": "\n".join(summary_lines),
            "batch_size": batch_size,
            "last_batch": last_batch,
            "best": best,
            "loop_iteration": loop_iteration,
        }

    def _set_optimize_field(self, task_name: str, field: str, value) -> bool:
        """Set or update a flat-key `optimize.<field>: <value>` line in task
        frontmatter. Inverse of `_clear_optimize_field`. Idempotent.
        """
        if not task_name:
            return False
        tasks_dir = self.project_root / "team-management" / "tasks"
        task_file = tasks_dir / f"{task_name}.md"
        if not task_file.exists():
            task_file = tasks_dir / task_name / "README.md"
        if not task_file.exists():
            return False
        try:
            content = task_file.read_text(encoding="utf-8")
            if not content.startswith("---"):
                return False
            end_marker = content.find("---", 3)
            if end_marker == -1:
                return False
            frontmatter = content[3:end_marker]
            body = content[end_marker + 3:]
            target_prefix = f"optimize.{field}:"
            new_line = f"optimize.{field}: {value}"
            lines = frontmatter.split("\n")
            replaced = False
            for i, line in enumerate(lines):
                if line.strip().startswith(target_prefix):
                    lines[i] = new_line
                    replaced = True
                    break
            if not replaced:
                # Insert before any trailing blank lines but inside frontmatter
                insert_pos = len(lines)
                for i in range(len(lines) - 1, -1, -1):
                    if lines[i].strip():
                        insert_pos = i + 1
                        break
                lines.insert(insert_pos, new_line)
            new_content = "---" + "\n".join(lines) + "---" + body
            task_file.write_text(new_content, encoding="utf-8")
            return True
        except (IOError, OSError):
            return False

    def _read_issue_provider(self) -> tuple:
        """Read `issue_tracking.provider` from team-management/config.json.

        Returns a tuple `(provider, source)`:
          - `(value, "configured")` — the disabled-provider 4-option menu is
            triggered. Reached two ways: (1) `issue_tracking.provider` is
            EXPLICITLY set (`("gitlab", "configured")`, `("disabled",
            "configured")`, …); (2) INFERRED disabled — the config has no
            `issue_tracking.provider` key (section absent, or a dict missing
            `provider`) AND no issue provider is enabled → `("disabled",
            "configured")`, so a fresh plugin project with no tracker gets the
            local-completion menu instead of the remote chain
            (m-fix-completion-strands-without-remote).
          - `("unknown", "unreadable")` — the config file is missing or not
            valid JSON. Caller falls back to the provider chain so a
            fat-finger edit of config.json does not block completion.
          - `("unknown", "legacy")` — run the legacy provider-driven chain.
            Reached when the config lacks `issue_tracking.provider` but a
            provider IS enabled (genuinely-old GitLab/Jira install), or when
            `issue_tracking` is present-but-non-dict (corruption — preserve
            the «malformed config never forces the menu» contract). An older
            enabled-provider install must keep its straight-line chain rather
            than be forced into the menu and fail with `completion_option is
            required` (Codex round-3 critical).
        """
        config_file = self.project_root / "team-management" / "config.json"
        if not config_file.exists():
            return ("unknown", "unreadable")
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)
        except (IOError, OSError, json.JSONDecodeError):
            return ("unknown", "unreadable")

        # Valid-JSON-wrong-shape (e.g. `[]`, `"oops"`, `null`, `42`) must fall
        # into the same fallback bucket as unreadable files — otherwise
        # `config.get("issue_tracking")` below raises AttributeError on a list
        # and completion fails before the legacy fallback can absorb it
        # (Codex round-4 warning).
        if not isinstance(config, dict):
            return ("unknown", "unreadable")

        # No explicit `issue_tracking.provider`. Pip-installer era: a missing
        # key meant a genuinely-old GitLab/Jira install → run the provider
        # chain. Plugin era: `/team-management:config` writes configs WITHOUT
        # an `issue_tracking` section at all, so an ABSENT section (or a dict
        # section missing `provider`) is usually "fresh user, no tracker" —
        # infer "disabled" so the 4-option local-completion menu shows instead
        # of forcing the remote provider chain (which strands a no-remote repo:
        # git fetch/push both fail). Only keep the legacy provider-driven
        # classification when a provider is ACTUALLY enabled
        # (m-fix-completion-strands-without-remote).
        #
        # A NON-DICT `issue_tracking` (e.g. `"oops"`) is corruption, not a
        # fresh config — preserve the existing "malformed config never forces
        # the menu" contract and fall through to the provider chain, mirroring
        # the top-level non-dict guard above.
        if "issue_tracking" not in config:
            if self._any_issue_provider_enabled(config):
                return ("unknown", "legacy")
            return ("disabled", "configured")

        issue_tracking = config.get("issue_tracking")
        if not isinstance(issue_tracking, dict):
            return ("unknown", "legacy")
        if "provider" not in issue_tracking:
            if self._any_issue_provider_enabled(config):
                return ("unknown", "legacy")
            return ("disabled", "configured")
        return (issue_tracking["provider"], "configured")

    @staticmethod
    def _any_issue_provider_enabled(config: Dict) -> bool:
        """True if any issue-tracking provider is explicitly enabled.

        Mirrors the MCP provider resolver's enabled-flag checks
        (mcp/core/config.py). Order is irrelevant — this is a boolean gate:
        a genuinely-old provider config (gitlab/jira/github enabled but
        predating the `issue_tracking.provider` key) stays on the
        provider-driven completion chain, while a no-tracker config falls
        through to the disabled-provider menu. isinstance-guarded so a
        malformed provider section (e.g. `gitlab: "x"`) cannot crash the probe.
        """
        for name in ("gitlab", "jira", "github"):
            section = config.get(name)
            if isinstance(section, dict) and section.get("enabled"):
                return True
        return False

    def _func_present_completion_options(self, args: Dict = None) -> Dict:
        """Pre-func for the completion step: when issue tracking is explicitly
        disabled, present a 4-option menu so the user can pick how to finish
        the task.

        When a provider is configured (gitlab/github/jira) OR the config is
        unreadable, this returns `skipped` and the provider-driven dispatcher
        flow runs unchanged on advance — preserving the pre-dispatcher
        behaviour for users whose config.json is malformed.
        """
        provider, source = self._read_issue_provider()

        if source in ("unreadable", "legacy"):
            # unreadable: fat-finger edit of config.json → run provider chain,
            # individual funcs handle missing config themselves.
            # legacy: older install without `issue_tracking.provider` key →
            # preserve the pre-dispatcher straight-line completion flow
            # (Codex round-3 critical).
            return {
                "func": "present_completion_options",
                "success": True,
                "skipped": (
                    f"config source='{source}' — preserving provider-driven flow; "
                    "individual funcs will handle any missing configuration."
                ),
                "provider": "unknown",
            }

        if provider != "disabled":
            return {
                "func": "present_completion_options",
                "success": True,
                "skipped": f"provider '{provider}' drives completion — no menu needed.",
                "provider": provider,
            }

        menu = (
            "Issue tracking is disabled — pick how to finish this task by calling\n"
            "`mcp__plugin_team-management_tm__protocol_advance(summary=..., args={\"completion_option\": \"<choice>\", ...})`.\n"
            "\n"
            "Options:\n"
            "  1. merge_local — commit, merge feature branch into the default branch locally, delete the feature branch, archive the task.\n"
            "     args: {\"completion_option\": \"merge_local\"}\n"
            "\n"
            "  2. push_pr — commit, push, open a pull request via `gh pr create`, archive the task. Requires the `gh` CLI.\n"
            "     args: {\"completion_option\": \"push_pr\"}\n"
            "\n"
            "  3. keep — commit, archive the task, keep the feature branch as-is (no merge, no push).\n"
            "     args: {\"completion_option\": \"keep\"}\n"
            "\n"
            "  4. discard — throw away ALL local work (uncommitted + untracked + gitignored via `git clean -fdx`), force-delete the feature branch. No archive, no commit, no push. PRESERVES the framework working tree: `team-management/` (config, other tasks, custom protocols) and `.claude/` (state, issue mappings, logs) are excluded from the clean.\n"
            "     Two-step: first call with {\"completion_option\": \"discard\"} returns a dry-run block.\n"
            "     Then re-advance with {\"completion_option\": \"discard\", \"discard_confirmation\": \"discard\", \"discard_confirmed_dry_run\": true}.\n"
            "     This is a friction mechanism against accidental invocation, NOT a security control.\n"
        )

        return {
            "func": "present_completion_options",
            "success": True,
            "provider": "disabled",
            "options": ["merge_local", "push_pr", "keep", "discard"],
            "message": menu,
        }

    def _func_require_discard_confirmation(self, args: Dict = None) -> Dict:
        """Post-func gate for the completion step: two-step typed confirmation
        before `completion_dispatch` is allowed to run the destructive discard
        path.

        FRICTION MECHANISM, NOT SECURITY. An LLM can trivially produce the
        string "discard" and the boolean True. The goal is to slow down
        accidental force-deletion — by the LLM, the human operator, or a copy-
        pasted `protocol_advance` call — not to stop a motivated actor. The
        real safety net is that the user must choose `discard` out of four
        visible options in the menu emitted by `_func_present_completion_options`.

        Semantics:
          * `completion_option != "discard"` → pass-through skip.
          * First call with `completion_option == "discard"` and no
            `discard_confirmed_dry_run` → return success=False with the exact
            re-advance arguments and the commit count that would be lost.
          * Second call with `discard_confirmation == "discard"` AND
            `discard_confirmed_dry_run is True` → success=True and the
            dispatcher proceeds.
        """
        args = args or {}
        if args.get("completion_option") != "discard":
            return {
                "func": "require_discard_confirmation",
                "success": True,
                "skipped": "not a discard operation",
            }

        task_state = get_task_state()
        branch = task_state.get("branch") or "<unknown>"

        # Use the shared detector so repos with custom default branches
        # (develop/trunk/etc.) get an accurate count in the dry-run message
        # — Codex round-8 caught this as a stale hardcoded main/master.
        default_branch = self._detect_default_branch()
        commits_ahead = "?"
        try:
            count_result = subprocess.run(
                ["git", "rev-list", "--count", f"{default_branch}..HEAD"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=GIT_TIMEOUT_FAST, check=False, cwd=str(self.project_root),
            )
            if count_result.returncode == 0:
                commits_ahead = count_result.stdout.strip() or "0"
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            # Best-effort — a missing count should not itself block the gate.
            commits_ahead = "?"

        if args.get("discard_confirmed_dry_run") is not True:
            return {
                "func": "require_discard_confirmation",
                "success": False,
                "error": (
                    f"Dry-run: would force-delete branch '{branch}' "
                    f"({commits_ahead} commits ahead of {default_branch}), "
                    f"hard-reset the working tree, and `git clean -fdx` all "
                    f"untracked AND gitignored files (build artifacts, "
                    f".env.local, editor caches, etc.). PRESERVED (excluded from "
                    f"the clean): the framework working tree — 'team-management/' "
                    f"(config, other tasks, custom protocols) and '.claude/' "
                    f"(state, issue mappings, logs). This cannot be undone "
                    f"from inside the tool. "
                    f"To confirm, re-advance with "
                    f"args={{'completion_option': 'discard', "
                    f"'discard_confirmation': 'discard', "
                    f"'discard_confirmed_dry_run': true}}. "
                    f"(Friction mechanism, not security.)"
                ),
                "branch": branch,
                "commits_ahead": commits_ahead,
            }

        if args.get("discard_confirmation") != "discard":
            return {
                "func": "require_discard_confirmation",
                "success": False,
                "error": (
                    "discard_confirmation must equal the exact string 'discard'. "
                    f"Got: {args.get('discard_confirmation')!r}."
                ),
            }

        return {
            "func": "require_discard_confirmation",
            "success": True,
            "branch": branch,
            "commits_ahead": commits_ahead,
            "message": "Discard confirmed. Dispatcher will force-delete the branch.",
        }

    def _func_completion_dispatch(self, args: Dict = None) -> Dict:
        """Post-func that executes the completion flow.

        Replaces the previous straight-line chain of completion post_funcs.
        Reads `issue_tracking.provider` from team-management/config.json:

          * provider != "disabled" — runs the provider-driven chain unchanged
            (archive → commit → merge-main → push → MR → issue status → cleanup
            → clear state → checkout default). This is byte-for-byte the old
            behaviour; existing GitLab/GitHub/Jira users see no difference.

          * provider == "disabled" — reads `args["completion_option"]` and
            dispatches to one of four local flows: merge_local / push_pr /
            keep / discard.

        Sub-func failures short-circuit the chain and return the first error.
        """
        args = args or {}

        # Optimize-protocol branch (T2): squash from optimize.best_commit and
        # inject the sorted TSV leaderboard into the squash commit message
        # before handing off to the existing provider chain.
        protocol_info = get_protocol_state() or {}
        if protocol_info.get("name") in ("optimize", "optimize-unattended"):
            return self._completion_optimize(args)

        provider, source = self._read_issue_provider()

        # Only the explicit `("disabled", "configured")` case triggers the
        # new 4-option menu. Anything else — a configured provider
        # (gitlab/github/jira), an unreadable config, or a legacy config
        # with no `issue_tracking.provider` key at all — runs the old
        # provider-driven chain. This preserves byte-for-byte behaviour for
        # upgrade paths where the user had a pre-dispatcher config and never
        # opted into the menu (Codex round-3 critical: absent key used to
        # run the legacy chain; forcing the menu blocked completion).
        if source != "configured" or provider != "disabled":
            chain = [
                ("archive_task", self._func_archive_task),
                ("git_commit", self._func_git_commit),
                ("git_merge_main", self._func_git_merge_main),
                ("git_push", self._func_git_push),
                ("create_merge_request", self._func_create_merge_request),
                ("update_issue_status", self._func_update_issue_status),
                ("cleanup_task_scoped_state", self._func_cleanup_task_scoped_state),
                ("clear_task_state", self._func_clear_task_state),
                ("checkout_default_branch", self._func_checkout_default_branch),
            ]
            return self._run_completion_chain("provider", chain, args)

        option = args.get("completion_option")
        if option is None:
            return {
                "func": "completion_dispatch",
                "success": False,
                "error": (
                    "completion_option is required when issue_tracking.provider == 'disabled'. "
                    "Choose one of: merge_local, push_pr, keep, discard. "
                    "See the 4-option menu from the completion step entry."
                ),
            }

        # Branch-safety precondition before dispatching any of the 4 local
        # flows. The destructive path (`discard`) and the committing paths
        # (`merge_local`, `push_pr`, `keep`) all read `feature_branch` from
        # task state — if the operator manually checked out a different
        # branch (e.g. `main`) before calling `protocol_advance`, running
        # `git reset --hard` / `git clean -fd` / `git commit` on that branch
        # is silent data loss on a branch the user never intended to touch
        # (Codex round-5 critical; Claude round-5 warning — both converge).
        task_state = get_task_state()
        expected_branch = task_state.get("branch")
        if not expected_branch:
            return {
                "func": "completion_dispatch",
                "success": False,
                "error": "No feature branch in task state — cannot dispatch local completion flow.",
            }
        # Option-injection guard: the feature branch flows into `git checkout`/
        # `merge`/`branch -D`/`gh pr` argv across all four local flows. Validate
        # once here (the single entry) BEFORE spending any git subprocess.
        if not validate_branch_name(expected_branch):
            return {
                "func": "completion_dispatch",
                "success": False,
                "error": (
                    f"Invalid feature branch name {expected_branch!r} in task state: "
                    f"only [A-Za-z0-9/._-] allowed, no leading '-'."
                ),
            }
        current_branch = self._current_branch()
        if current_branch != expected_branch:
            head_desc = (
                "detached HEAD (no branch)" if current_branch is None
                else f"'{current_branch}'"
            )
            return {
                "func": "completion_dispatch",
                "success": False,
                "branch_taken": option,
                "error": (
                    f"Branch mismatch: HEAD is {head_desc} but task expects "
                    f"'{expected_branch}'. Refusing to run '{option}' on the wrong branch. "
                    f"Either checkout '{expected_branch}' and retry, or abort the completion "
                    f"step."
                ),
                "current_branch": current_branch,
                "expected_branch": expected_branch,
            }

        if option == "merge_local":
            return self._completion_merge_local(args)
        if option == "push_pr":
            return self._completion_push_pr(args)
        if option == "keep":
            return self._completion_keep(args)
        if option == "discard":
            return self._completion_discard(args)

        return {
            "func": "completion_dispatch",
            "success": False,
            "error": (
                f"Unknown completion_option {option!r}. "
                f"Valid options: merge_local, push_pr, keep, discard."
            ),
        }

    def _run_completion_chain(self, branch_taken: str, chain, args: Dict) -> Dict:
        """Run a list of (name, callable) completion sub-funcs and aggregate
        results. Stops at the first failure and returns it. Each callable is
        an `_func_*` method with signature `(self, args: Dict = None)` — all
        of them tolerate the `args` dict even when they ignore it.
        """
        sub_results: List[Dict] = []
        for name, fn in chain:
            result = fn(args)
            sub_results.append(result)
            if not result.get("success"):
                return {
                    "func": "completion_dispatch",
                    "success": False,
                    "branch_taken": branch_taken,
                    "failed_at": name,
                    "error": result.get("error") or f"{name} failed without an error message",
                    "sub_results": sub_results,
                }
        return {
            "func": "completion_dispatch",
            "success": True,
            "branch_taken": branch_taken,
            "sub_results": sub_results,
        }

    def _completion_optimize(self, args: Dict) -> Dict:
        """Optimize-protocol completion: squash from optimize.best_commit so the
        reviewer sees a clean baseline-to-best diff; inject the sorted TSV
        leaderboard into the squash commit message; then hand off to the
        existing provider/disabled chain (skipping git_commit since the squash
        already created the commit on the feature branch).

        Branch-safety precondition (mirrors the disabled-provider local flows):
        HEAD must match the task's feature branch before any reset/checkout.
        """
        task_state = get_task_state()
        task_name = task_state.get("task")
        expected_branch = task_state.get("branch")

        if not expected_branch:
            return {"func": "completion_dispatch", "success": False,
                    "branch_taken": "optimize",
                    "error": "No feature branch in task state — cannot complete optimize protocol."}
        # Option-injection guard before the branch reaches any squash/checkout argv.
        if not validate_branch_name(expected_branch):
            return {"func": "completion_dispatch", "success": False,
                    "branch_taken": "optimize",
                    "error": (
                        f"Invalid feature branch name {expected_branch!r} in task state: "
                        f"only [A-Za-z0-9/._-] allowed, no leading '-'."
                    )}
        current_branch = self._current_branch()
        if current_branch != expected_branch:
            head_desc = "detached HEAD" if current_branch is None else f"'{current_branch}'"
            return {"func": "completion_dispatch", "success": False,
                    "branch_taken": "optimize",
                    "error": (
                        f"Branch mismatch: HEAD is {head_desc} but task expects "
                        f"'{expected_branch}'. Refusing to squash on the wrong branch."
                    ),
                    "current_branch": current_branch,
                    "expected_branch": expected_branch}

        # Read squash anchors from frontmatter
        fm = parse_task_frontmatter(task_name) or {}
        best_commit = (fm.get("optimize.best_commit") or "").strip()
        baseline_commit = (fm.get("optimize.baseline_commit") or "").strip()

        if not best_commit or best_commit == "-":
            return {"func": "completion_dispatch", "success": False,
                    "branch_taken": "optimize",
                    "error": "No optimize.best_commit recorded in task frontmatter. Run experimentation first."}
        if not baseline_commit or baseline_commit == "-":
            return {"func": "completion_dispatch", "success": False,
                    "branch_taken": "optimize",
                    "error": "No optimize.baseline_commit recorded in task frontmatter. Re-run capture_metric_baseline."}
        if best_commit == baseline_commit:
            return {"func": "completion_dispatch", "success": False,
                    "branch_taken": "optimize",
                    "error": "best_commit equals baseline_commit — no improvement found, nothing to squash."}

        # Build leaderboard markdown
        leaderboard_md = self._build_leaderboard(task_name)

        # Pull human-readable title from the task file H1 (after frontmatter)
        h1_title = task_name
        task_file = self.project_root / "team-management" / "tasks" / f"{task_name}.md"
        if not task_file.exists():
            task_file = self.project_root / "team-management" / "tasks" / task_name / "README.md"
        if task_file.exists():
            try:
                content = task_file.read_text(encoding="utf-8")
                end = content.find("---", 3)
                body = content[end + 3:] if end != -1 else content
                for line in body.split("\n"):
                    if line.startswith("# "):
                        h1_title = line[2:].strip()
                        break
            except (IOError, OSError):
                pass

        commit_msg = (
            f"{h1_title}\n\n"
            f"Squashed from optimize.best_commit={best_commit} "
            f"(baseline={baseline_commit}).\n\n"
            f"## Leaderboard\n\n"
            f"{leaderboard_md}\n"
        )

        # Validate the user's chosen completion path BEFORE the destructive
        # squash. Round-2 review (Codex C1 / Claude W1): putting `_squash_to_best`
        # before option validation meant a missing-option error or a discard
        # dry-run still rewrote the branch — defeats the typed-confirmation
        # gate. The non-optimize completion path validates first, then squashes.
        provider, source = self._read_issue_provider()
        chain_args = dict(args)
        chain_args["leaderboard"] = leaderboard_md
        chain_args["squashed_from"] = best_commit
        chain_args["squashed_baseline"] = baseline_commit

        is_disabled = (source == "configured" and provider == "disabled")
        option = chain_args.get("completion_option") if is_disabled else None

        if is_disabled:
            if option is None:
                return {
                    "func": "completion_dispatch",
                    "success": False,
                    "branch_taken": "optimize",
                    "error": (
                        "completion_option is required when issue_tracking.provider == 'disabled'. "
                        "Choose one of: merge_local, push_pr, keep, discard."
                    ),
                    "squashed_from": best_commit,
                    "squashed_baseline": baseline_commit,
                }
            if option not in ("merge_local", "push_pr", "keep", "discard"):
                return {
                    "func": "completion_dispatch",
                    "success": False,
                    "branch_taken": "optimize",
                    "error": (
                        f"Unknown completion_option {option!r}. "
                        f"Valid options: merge_local, push_pr, keep, discard."
                    ),
                }
            if option == "discard":
                # Run the discard dry-run / typed-confirmation gate BEFORE
                # the squash so the dry-run does not mutate the branch.
                discard_gate = self._func_require_discard_confirmation(chain_args)
                if not discard_gate.get("success"):
                    return {
                        "func": "completion_dispatch",
                        "success": False,
                        "branch_taken": "optimize",
                        "error": discard_gate.get("error"),
                        "discard_gate": discard_gate,
                    }

        # Squash unless we're about to discard the branch entirely. The
        # discard path deletes the feature branch — running an irreversible
        # `git reset --hard` first would mutate history pointlessly and
        # leave the branch in a partial-failure state if `_completion_discard`
        # later fails at `checkout_default_branch` / `git branch -D`
        # (Codex round-3 critical).
        skip_squash_for_discard = is_disabled and option == "discard"

        if not skip_squash_for_discard:
            squash_result = self._squash_to_best(best_commit, baseline_commit, commit_msg)
            if not squash_result["success"]:
                return {"func": "completion_dispatch", "success": False,
                        "branch_taken": "optimize",
                        "error": squash_result["error"],
                        "squash_result": squash_result}

        # Hand off — skip the standalone git_commit only on the squash itself,
        # but include git_commit AFTER archive_task so the archive rename is
        # committed (Codex round-2 critical: archive_task renames the task file
        # but optimize chain previously skipped git_commit, leaving the working
        # tree dirty for git_merge_main).
        if is_disabled:
            if option == "discard":
                result = self._completion_discard(chain_args)
            elif option == "merge_local":
                result = self._completion_merge_local(chain_args)
            elif option == "push_pr":
                result = self._completion_push_pr(chain_args)
            else:  # "keep"
                result = self._completion_keep(chain_args)
            if result.get("success"):
                if not skip_squash_for_discard:
                    result["squashed_from"] = best_commit
                    result["squashed_baseline"] = baseline_commit
                result["leaderboard"] = leaderboard_md
            return result

        # Provider-driven path: provider configured (gitlab/github/jira) or
        # legacy / unreadable config — preserve existing behaviour but include
        # git_commit AFTER archive_task to commit the archive rename.
        chain = [
            ("archive_task", self._func_archive_task),
            ("git_commit", self._func_git_commit),
            ("git_merge_main", self._func_git_merge_main),
            ("git_push", self._func_git_push),
            ("create_merge_request", self._func_create_merge_request),
            ("update_issue_status", self._func_update_issue_status),
            ("cleanup_task_scoped_state", self._func_cleanup_task_scoped_state),
            ("clear_task_state", self._func_clear_task_state),
            ("checkout_default_branch", self._func_checkout_default_branch),
        ]

        chain_result = self._run_completion_chain("optimize", chain, chain_args)
        if chain_result.get("success"):
            chain_result["squashed_from"] = best_commit
            chain_result["squashed_baseline"] = baseline_commit
            chain_result["leaderboard"] = leaderboard_md
        return chain_result

    def _squash_to_best(self, best_commit: str, baseline_commit: str, commit_msg: str) -> Dict:
        """Squash the current feature branch so its tree exactly matches
        `best_commit` and its parent is `baseline_commit`. The previous
        implementation used `git checkout <best> -- .` which only restores
        modified/added files — files DELETED in `best_commit` would leak
        through (Codex round-1 critical). The correct sequence is:

            1. `git reset --hard <best_commit>` — index + working tree
               exactly match best_commit (including deletions).
            2. `git reset --soft <baseline_commit>` — branch pointer moves
               to baseline; index + working tree unchanged (still match best).
            3. `git commit` — single squash commit whose tree is best and
               whose parent is baseline.

        On any step failure we capture the original HEAD and reset --hard
        back to it so the user is not left in a half-rewound state.
        """
        # Capture original HEAD for rollback on any failure
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=30,
                cwd=str(self.project_root), check=False,
            )
            original_head = proc.stdout.strip() if proc.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            original_head = None

        # Without a captured HEAD there is no rollback point — abort before
        # any reset rather than proceeding with a silently no-op rollback
        # (R2-O1).
        if not original_head:
            return {"success": False,
                    "error": "could not capture original HEAD (git rev-parse failed) — "
                             "aborting squash before any reset; resolve the git error "
                             "and re-run completion"}

        def _rollback(reason: str) -> None:
            if original_head:
                try:
                    subprocess.run(
                        ["git", "reset", "--hard", original_head],
                        capture_output=True, text=True, timeout=30,
                        cwd=str(self.project_root), check=False,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass

        # 1. Hard-reset to best_commit so working tree matches it (including deletions)
        try:
            proc = subprocess.run(
                ["git", "reset", "--hard", best_commit],
                capture_output=True, text=True, timeout=60,
                cwd=str(self.project_root), check=False,
            )
            if proc.returncode != 0:
                _rollback("reset-hard-failed")
                return {"success": False,
                        "error": f"git reset --hard {best_commit} failed: {proc.stderr.strip()}"}
        except (OSError, subprocess.TimeoutExpired) as e:
            _rollback("reset-hard-exception")
            return {"success": False, "error": f"git reset --hard failed: {e}"}

        # 2. Soft-reset to baseline_commit (move branch pointer back; keep tree)
        try:
            proc = subprocess.run(
                ["git", "reset", "--soft", baseline_commit],
                capture_output=True, text=True, timeout=60,
                cwd=str(self.project_root), check=False,
            )
            if proc.returncode != 0:
                _rollback("reset-soft-failed")
                return {"success": False,
                        "error": f"git reset --soft {baseline_commit} failed: {proc.stderr.strip()}"}
        except (OSError, subprocess.TimeoutExpired) as e:
            _rollback("reset-soft-exception")
            return {"success": False, "error": f"git reset --soft failed: {e}"}

        # 3. Single squash commit
        try:
            proc = subprocess.run(
                ["git", "commit", "--allow-empty", "-m", commit_msg],
                capture_output=True, text=True, timeout=60,
                cwd=str(self.project_root), check=False,
            )
            if proc.returncode != 0:
                _rollback("commit-failed")
                return {"success": False,
                        "error": f"git commit failed: {proc.stderr.strip() or proc.stdout.strip()}"}
        except (OSError, subprocess.TimeoutExpired) as e:
            _rollback("commit-exception")
            return {"success": False, "error": f"git commit failed: {e}"}

        return {"success": True}

    def _build_leaderboard(self, task_name: str) -> str:
        """Read results.tsv, sort by metric per direction, take top 10, format
        as a markdown table. Used by `_completion_optimize` to inject into the
        squash commit message and downstream MR/PR description."""
        rows = self._read_results_tsv_rows(task_name)
        ok_rows = [r for r in rows if r.get("status") == "ok" and r["iteration"] >= 0]
        if not ok_rows:
            return "_(no ok rows in results.tsv)_"
        state = self._read_optimize_state() or {}
        direction = state.get("metric_direction", "min")
        sorted_rows = sorted(
            ok_rows,
            key=lambda r: r["metric_value"],
            reverse=(direction == "max"),
        )
        top = sorted_rows[:10]
        lines = [
            "| Rank | Iter | Metric | Commit | Hypothesis |",
            "| ---- | ---- | ------ | ------ | ---------- |",
        ]
        for rank, r in enumerate(top, 1):
            hyp = (r.get("hypothesis") or "").replace("|", "\\|")
            if len(hyp) > 80:
                hyp = hyp[:77] + "…"
            lines.append(
                f"| {rank} | {r['iteration']} | {r['metric_value']} | "
                f"{r.get('commit_sha', '-')} | {hyp} |"
            )
        return "\n".join(lines)

    def _completion_merge_local(self, args: Dict) -> Dict:
        """Local flow: commit, checkout default, merge feature, delete feature,
        archive, cleanup."""
        task_state = get_task_state()
        feature_branch = task_state.get("branch")
        if not feature_branch:
            return {
                "func": "completion_dispatch",
                "success": False,
                "branch_taken": "merge_local",
                "error": "No feature branch in task state — cannot merge.",
            }

        sub_results: List[Dict] = []

        # 1. archive task file on feature branch so the move is part of the
        #    commit (mirrors the provider flow).
        archive_res = self._func_archive_task()
        sub_results.append(archive_res)
        if not archive_res.get("success"):
            return self._completion_fail("merge_local", "archive_task", archive_res, sub_results)

        # 2. commit (includes archive move + any remaining work).
        commit_res = self._func_git_commit()
        sub_results.append(commit_res)
        if not commit_res.get("success"):
            return self._completion_fail("merge_local", "git_commit", commit_res, sub_results)

        # 3. checkout default.
        checkout_res = self._func_checkout_default_branch()
        sub_results.append(checkout_res)
        if not checkout_res.get("success"):
            return self._completion_fail("merge_local", "checkout_default_branch", checkout_res, sub_results)
        default_branch = checkout_res.get("branch", "main")

        # 4. merge feature into default (no-ff to preserve history).
        merge_res = self._git_merge_feature(feature_branch, default_branch)
        sub_results.append(merge_res)
        if not merge_res.get("success"):
            return self._completion_fail("merge_local", "merge_feature", merge_res, sub_results)

        # 5. delete feature branch (safe: -d refuses unmerged branches).
        delete_res = self._git_delete_feature(feature_branch, force=False)
        sub_results.append(delete_res)
        if not delete_res.get("success"):
            return self._completion_fail("merge_local", "delete_feature", delete_res, sub_results)

        # 6. cleanup + clear state.
        cleanup_res = self._func_cleanup_task_scoped_state()
        sub_results.append(cleanup_res)
        clear_res = self._func_clear_task_state()
        sub_results.append(clear_res)

        return {
            "func": "completion_dispatch",
            "success": True,
            "branch_taken": "merge_local",
            "sub_results": sub_results,
        }

    def _completion_push_pr(self, args: Dict) -> Dict:
        """Local flow: commit, push, `gh pr create`, archive, cleanup, checkout
        default. Requires the `gh` CLI."""
        if shutil.which("gh") is None:
            return {
                "func": "completion_dispatch",
                "success": False,
                "branch_taken": "push_pr",
                "error": (
                    "`gh` CLI not found on PATH. Install it and re-authenticate:\n"
                    "  macOS:   brew install gh && gh auth login\n"
                    "  Linux:   apt install gh  (or follow https://cli.github.com)\n"
                    "  Windows: winget install --id GitHub.cli && gh auth login"
                ),
            }

        task_state = get_task_state()
        task_name = task_state.get("task") or "unknown-task"
        feature_branch = task_state.get("branch")
        if not feature_branch:
            return {
                "func": "completion_dispatch",
                "success": False,
                "branch_taken": "push_pr",
                "error": "No feature branch in task state — cannot push PR.",
            }

        sub_results: List[Dict] = []

        archive_res = self._func_archive_task()
        sub_results.append(archive_res)
        if not archive_res.get("success"):
            return self._completion_fail("push_pr", "archive_task", archive_res, sub_results)

        commit_res = self._func_git_commit()
        sub_results.append(commit_res)
        if not commit_res.get("success"):
            return self._completion_fail("push_pr", "git_commit", commit_res, sub_results)

        push_res = self._func_git_push()
        sub_results.append(push_res)
        if not push_res.get("success"):
            return self._completion_fail("push_pr", "git_push", push_res, sub_results)

        # Compose PR title + body from the task file (or its archived copy).
        title, body = self._derive_pr_title_body(task_name, feature_branch)
        default_branch = self._detect_default_branch()

        # Idempotency pre-check (Codex round-3 warning): if a previous
        # dispatch already opened the PR and then failed in post-PR
        # housekeeping, a naive retry calls `gh pr create` again and exits
        # non-zero with "a pull request for branch X already exists".
        # Detect that state up-front and reuse the existing URL.
        existing_pr_url = self._gh_find_existing_pr(feature_branch)
        if existing_pr_url is not None:
            pr_url = existing_pr_url
            sub_results.append({
                "func": "gh_pr_create",
                "success": True,
                "pr_url": pr_url,
                "reused_existing": True,
            })
        else:
            try:
                pr_result = subprocess.run(
                    [
                        "gh", "pr", "create",
                        "--title", title,
                        "--body", body,
                        "--base", default_branch,
                        "--head", feature_branch,
                    ],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=GIT_TIMEOUT_SLOW,
                    check=False,
                    cwd=str(self.project_root),
                )
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
                pr_fail = {"func": "gh_pr_create", "success": False, "error": str(e)}
                sub_results.append(pr_fail)
                return self._completion_fail("push_pr", "gh_pr_create", pr_fail, sub_results)

            if pr_result.returncode != 0:
                pr_fail = {
                    "func": "gh_pr_create",
                    "success": False,
                    "error": f"gh pr create failed (exit {pr_result.returncode}): {pr_result.stderr.strip()}",
                }
                sub_results.append(pr_fail)
                return self._completion_fail("push_pr", "gh_pr_create", pr_fail, sub_results)

            pr_url = pr_result.stdout.strip().splitlines()[-1] if pr_result.stdout.strip() else "(created)"
            sub_results.append({"func": "gh_pr_create", "success": True, "pr_url": pr_url})

        # Post-PR housekeeping: each step must succeed before the dispatcher
        # claims a clean completion. Order matters — `checkout_default_branch`
        # runs BEFORE `clear_task_state` so a checkout failure leaves the
        # task state intact and the user can retry cleanly. Previously the
        # order was cleanup → clear → checkout, which stranded users on the
        # feature branch with current_task.json already wiped when checkout
        # failed (Codex warning, round 2). Cleanup runs first because it's
        # the least risky; clear runs last because it's the last thing we
        # want to discard if something goes wrong.
        cleanup_res = self._func_cleanup_task_scoped_state()
        sub_results.append(cleanup_res)
        if not cleanup_res.get("success"):
            return self._completion_fail("push_pr", "cleanup_task_scoped_state", cleanup_res, sub_results)

        checkout_res = self._func_checkout_default_branch()
        sub_results.append(checkout_res)
        if not checkout_res.get("success"):
            return self._completion_fail("push_pr", "checkout_default_branch", checkout_res, sub_results)

        clear_res = self._func_clear_task_state()
        sub_results.append(clear_res)
        if not clear_res.get("success"):
            return self._completion_fail("push_pr", "clear_task_state", clear_res, sub_results)

        return {
            "func": "completion_dispatch",
            "success": True,
            "branch_taken": "push_pr",
            "pr_url": pr_url,
            "sub_results": sub_results,
        }

    def _completion_keep(self, args: Dict) -> Dict:
        """Local flow: commit, archive, cleanup. Feature branch preserved
        (checked out, no merge, no push)."""
        sub_results: List[Dict] = []

        archive_res = self._func_archive_task()
        sub_results.append(archive_res)
        if not archive_res.get("success"):
            return self._completion_fail("keep", "archive_task", archive_res, sub_results)

        commit_res = self._func_git_commit()
        sub_results.append(commit_res)
        if not commit_res.get("success"):
            return self._completion_fail("keep", "git_commit", commit_res, sub_results)

        cleanup_res = self._func_cleanup_task_scoped_state()
        sub_results.append(cleanup_res)
        clear_res = self._func_clear_task_state()
        sub_results.append(clear_res)

        return {
            "func": "completion_dispatch",
            "success": True,
            "branch_taken": "keep",
            "sub_results": sub_results,
        }

    def _completion_discard(self, args: Dict) -> Dict:
        """Local flow: throw away uncommitted work, checkout default, force-
        delete feature branch. No archive, no commit, no push.
        `require_discard_confirmation` has already validated intent."""
        task_state = get_task_state()
        feature_branch = task_state.get("branch")
        if not feature_branch:
            return {
                "func": "completion_dispatch",
                "success": False,
                "branch_taken": "discard",
                "error": "No feature branch in task state — nothing to discard.",
            }

        sub_results: List[Dict] = []
        cwd = str(self.project_root)

        # 1. Throw away any uncommitted changes on feature branch.
        try:
            reset_res = subprocess.run(
                ["git", "reset", "--hard", "HEAD"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=GIT_TIMEOUT_MEDIUM, check=False, cwd=cwd,
            )
            sub_results.append({
                "func": "git_reset_hard",
                "success": reset_res.returncode == 0,
                "stderr": reset_res.stderr.strip(),
            })
            if reset_res.returncode != 0:
                return self._completion_fail("discard", "git_reset_hard",
                                             {"error": reset_res.stderr.strip()}, sub_results)

            # -fdx (not -fd): also remove gitignored files so "discard" matches
            # the documented contract ("throw away all uncommitted work"). With
            # only -fd, build artifacts, .env.local, editor caches, etc. would
            # survive and subsequent checkout-default could fail with "untracked
            # working tree files would be overwritten" (Codex round-final).
            #
            # -e team-management/ -e .claude/: the framework gitignores its OWN
            # state — team-management/config.json (config flow) and all of
            # .claude/ (session-start) — so an un-excluded -fdx would wipe config
            # + every .claude/state/*-mappings.json (task<->issue links,
            # irrecoverable) + logs, AND any UNTRACKED sibling task under
            # team-management/tasks/ or custom protocol under
            # team-management/protocol-configs/custom/. Excluding both dirs
            # wholesale preserves the framework working tree (a superset of just
            # config.json). Discarding ONE task must never destroy config,
            # mappings, sibling tasks, or custom protocols. Safe for the later
            # checkout: both dirs are gitignored, so a checkout won't touch them.
            # Accepted nuance: the discarded task's own untracked task file may
            # remain behind as harmless litter (h-fix-discard-clean-and-windows-transcript).
            clean_res = subprocess.run(
                ["git", "clean", "-fdx", "-e", "team-management/", "-e", ".claude/"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=GIT_TIMEOUT_MEDIUM, check=False, cwd=cwd,
            )
            sub_results.append({
                "func": "git_clean",
                "success": clean_res.returncode == 0,
                "stderr": clean_res.stderr.strip(),
            })
            if clean_res.returncode != 0:
                # Untracked files could not be cleaned (permission error, nested
                # repo, locked file on Windows, etc.). Short-circuit before the
                # branch is deleted so the operator sees the partial-state error
                # rather than a false success. Codex warning, fixed.
                return self._completion_fail("discard", "git_clean",
                                             {"error": clean_res.stderr.strip()}, sub_results)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            return self._completion_fail("discard", "git_reset_hard",
                                         {"error": str(e)}, sub_results)

        # 2. checkout default branch.
        checkout_res = self._func_checkout_default_branch()
        sub_results.append(checkout_res)
        if not checkout_res.get("success"):
            return self._completion_fail("discard", "checkout_default_branch", checkout_res, sub_results)

        # 3. force-delete feature branch.
        delete_res = self._git_delete_feature(feature_branch, force=True)
        sub_results.append(delete_res)
        if not delete_res.get("success"):
            return self._completion_fail("discard", "delete_feature", delete_res, sub_results)

        # 4. cleanup + clear state. No archive — the task file went with the branch.
        cleanup_res = self._func_cleanup_task_scoped_state()
        sub_results.append(cleanup_res)
        clear_res = self._func_clear_task_state()
        sub_results.append(clear_res)

        return {
            "func": "completion_dispatch",
            "success": True,
            "branch_taken": "discard",
            "branch_deleted": feature_branch,
            "sub_results": sub_results,
        }

    def _completion_fail(self, branch_taken: str, failed_at: str, result: Dict, sub_results: List[Dict]) -> Dict:
        return {
            "func": "completion_dispatch",
            "success": False,
            "branch_taken": branch_taken,
            "failed_at": failed_at,
            "error": result.get("error") or f"{failed_at} failed",
            "sub_results": sub_results,
        }

    def _git_merge_feature(self, feature_branch: str, default_branch: str) -> Dict:
        try:
            result = subprocess.run(
                ["git", "merge", "--no-ff", "-m", f"Merge branch '{feature_branch}'", feature_branch],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=GIT_TIMEOUT_MEDIUM, check=False, cwd=str(self.project_root),
            )
            if result.returncode != 0:
                return {
                    "func": "merge_feature",
                    "success": False,
                    "error": (
                        f"git merge of '{feature_branch}' into '{default_branch}' failed: "
                        f"{result.stderr.strip()}"
                    ),
                }
            return {
                "func": "merge_feature",
                "success": True,
                "feature_branch": feature_branch,
                "default_branch": default_branch,
            }
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            return {"func": "merge_feature", "success": False, "error": str(e)}

    def _git_delete_feature(self, feature_branch: str, *, force: bool) -> Dict:
        flag = "-D" if force else "-d"
        try:
            result = subprocess.run(
                ["git", "branch", flag, feature_branch],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=GIT_TIMEOUT_FAST, check=False, cwd=str(self.project_root),
            )
            if result.returncode != 0:
                return {
                    "func": "delete_feature",
                    "success": False,
                    "error": f"git branch {flag} {feature_branch} failed: {result.stderr.strip()}",
                    "force": force,
                }
            return {
                "func": "delete_feature",
                "success": True,
                "branch": feature_branch,
                "force": force,
            }
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            return {"func": "delete_feature", "success": False, "error": str(e), "force": force}

    def _gh_find_existing_pr(self, feature_branch: str) -> Optional[str]:
        """Return the URL of an open PR for `feature_branch`, or None if none
        exists. Used by `_completion_push_pr` for idempotent retry after a
        post-PR housekeeping failure.

        Silent best-effort: if `gh` is missing, auth fails, or the response
        is malformed, returns None so the caller proceeds with `gh pr create`
        and surfaces any error there.
        """
        try:
            result = subprocess.run(
                ["gh", "pr", "view", feature_branch, "--json", "url,state"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_MEDIUM,
                check=False,
                cwd=str(self.project_root),
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None

        if result.returncode != 0:
            return None

        try:
            data = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError):
            return None

        if data.get("state") != "OPEN":
            return None
        url = data.get("url")
        return url if isinstance(url, str) and url else None

    def _current_branch(self) -> Optional[str]:
        """Return the current git branch name, or None if unavailable
        (detached HEAD, not a git repo, subprocess error). Used as a
        precondition by `_func_completion_dispatch` to refuse running
        destructive flows on the wrong branch.
        """
        try:
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_FAST,
                check=False,
                cwd=str(self.project_root),
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None
        if result.returncode != 0:
            return None
        name = result.stdout.strip()
        return name or None

    def _detect_default_branch(self) -> str:
        """Return the repository's default branch name.

        Thin wrapper over the shared hooks-level ``detect_default_branch``
        (git_operations.py) — the single source of truth shared with the MCP
        ``git_operations`` tool. Kept as a method so callers/tests that patch
        ``self._detect_default_branch`` keep working.
        """
        return detect_default_branch(self.project_root)

    def _derive_pr_title_body(self, task_name: str, feature_branch: str) -> tuple:
        title = f"feat: {task_name}"
        body = f"Closes task: `{task_name}`.\n\nBranch: `{feature_branch}`."

        base = self.project_root / "team-management"
        for candidate in (
            base / "tasks" / f"{task_name}.md",
            base / "tasks" / "done" / f"{task_name}.md",
            base / "tasks" / task_name / "README.md",
            base / "tasks" / "done" / task_name / "README.md",
        ):
            if not candidate.exists():
                continue
            try:
                text = candidate.read_text(encoding="utf-8")
            except (IOError, OSError):
                continue
            for line in text.splitlines():
                if line.startswith("# "):
                    title = line.replace("# ", "", 1).strip() or title
                    break
            body = (
                f"Closes task: `{task_name}`.\n\n"
                f"Branch: `{feature_branch}`.\n\n"
                f"See `{candidate.relative_to(self.project_root)}` for the full task spec."
            )
            return title, body
        return title, body

