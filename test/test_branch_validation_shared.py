#!/usr/bin/env python3
"""l-fix-security-hardening-residuals — Finding 1: one shared branch-name validator.

The option-injection guard (`^[a-zA-Z0-9/._-]+$` + leading-dash reject) is now a
single source of truth in `plugin/hooks/git_operations.py::validate_branch_name`,
consumed by the MCP git tools AND the engine/optimize git funcs. These tests
prove:
  - the shared validator's truth table (accepts framework prefixes, rejects
    leading-dash / metachar / empty),
  - the ENGINE path (`_func_git_setup_branch`) rejects a `-`-prefixed and a
    metachar branch before any git subprocess runs,
  - the optimize/local completion dispatcher rejects an invalid task-state
    branch at its branch-safety precondition,
  - the MCP `git_push` tool and `code_review_utils.validate_branch_name` still
    reject the same names (now via the shared helper).

Run with: python3 -m pytest test/test_branch_validation_shared.py -v
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Callable, Dict
from unittest.mock import MagicMock, patch

_REPO = Path(__file__).resolve().parent.parent
_MCP_DIR = _REPO / "plugin" / "mcp"
_HOOKS_DIR = _REPO / "plugin" / "hooks"
for _p in (str(_REPO), str(_MCP_DIR), str(_HOOKS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import shared_state  # noqa: E402
from git_operations import validate_branch_name  # noqa: E402
from protocol_engine import ProtocolEngine  # noqa: E402


# ---------------------------------------------------------------------------
# Shared validator truth table
# ---------------------------------------------------------------------------

class TestSharedValidator(unittest.TestCase):
    def test_accepts_framework_prefixes_and_dotted(self):
        for good in ("fix/security-hardening-residuals", "feature/x", "optimize/o-tune",
                     "brainstorm/b-idea", "experiment/e1", "release/1.0.0", "main",
                     "a_b.c-d/e"):
            self.assertTrue(validate_branch_name(good), good)

    def test_rejects_leading_dash_metachar_and_empty(self):
        for bad in ("-x", "--force", "-D", "a;b", "a b", "a$b", "a|b", "a`b",
                    "a&b", "a(b)", "a\nb", "", "  "):
            self.assertFalse(validate_branch_name(bad), repr(bad))

    def test_rejects_none_and_non_string_safely(self):
        # A truthy non-string task-state value must not raise (no .startswith).
        for bad in (None, 123, ["fix/x"], {"b": 1}, object()):
            self.assertFalse(validate_branch_name(bad), repr(bad))


# ---------------------------------------------------------------------------
# Engine harness (temp project + patched shared_state path globals)
# ---------------------------------------------------------------------------

class _EngineBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        (self.temp_dir / ".claude" / "state" / "protocol-logs").mkdir(parents=True)
        (self.temp_dir / ".claude" / "state" / "tasks").mkdir(parents=True)
        (self.temp_dir / "team-management" / "tasks").mkdir(parents=True)
        self._write_json(
            self.temp_dir / ".claude" / "state" / "current_task.json",
            {"task": None, "branch": None, "services": [], "updated": "2026-07-14"},
        )
        self._write_json(
            self.temp_dir / ".claude" / "state" / "daic-mode.json", {"mode": "discussion"},
        )
        self._orig = {
            k: getattr(shared_state, k) for k in
            ("PROJECT_ROOT", "STATE_DIR", "TASK_STATE_FILE", "DAIC_STATE_FILE", "PROTOCOL_LOGS_DIR")
        }
        shared_state.PROJECT_ROOT = self.temp_dir
        shared_state.STATE_DIR = self.temp_dir / ".claude" / "state"
        shared_state.TASK_STATE_FILE = self.temp_dir / ".claude" / "state" / "current_task.json"
        shared_state.DAIC_STATE_FILE = self.temp_dir / ".claude" / "state" / "daic-mode.json"
        shared_state.PROTOCOL_LOGS_DIR = self.temp_dir / ".claude" / "state" / "protocol-logs"
        self.engine = ProtocolEngine(self.temp_dir)

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(shared_state, k, v)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_json(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _write_config(self, **keys) -> None:
        self._write_json(self.temp_dir / "team-management" / "config.json", keys)

    def _set_task_state(self, task, branch) -> None:
        self._write_json(
            self.temp_dir / ".claude" / "state" / "current_task.json",
            {"task": task, "branch": branch, "services": [], "updated": "2026-07-14"},
        )


class TestEngineGitSetupBranchValidation(_EngineBase):
    def test_rejects_leading_dash_before_any_git(self):
        # Patch subprocess.run to prove the validator returns BEFORE any git op.
        with patch("protocol_engine.subprocess.run") as run_mock:
            result = self.engine._func_git_setup_branch(args={"branch": "-D"})
        self.assertFalse(result["success"])
        self.assertIn("Invalid branch name", result["error"])
        run_mock.assert_not_called()

    def test_rejects_metachar_before_any_git(self):
        with patch("protocol_engine.subprocess.run") as run_mock:
            result = self.engine._func_git_setup_branch(args={"branch": "fix/x;rm -rf /"})
        self.assertFalse(result["success"])
        self.assertIn("Invalid branch name", result["error"])
        run_mock.assert_not_called()

    def test_valid_branch_passes_the_validation_gate(self):
        # A valid name must NOT be rejected by the validator gate — it proceeds
        # to real git (mocked here to a clean-tree / already-on-branch path).
        clean = MagicMock(returncode=0, stdout="", stderr="")
        with patch("protocol_engine.subprocess.run", return_value=clean):
            result = self.engine._func_git_setup_branch(args={"branch": "fix/security-hardening-residuals"})
        # Whatever the git outcome, the failure (if any) must not be the
        # invalid-branch-name gate.
        self.assertNotIn("Invalid branch name", result.get("error", ""))


class TestCompletionDispatchBranchValidation(_EngineBase):
    def test_dispatch_rejects_invalid_task_state_branch(self):
        self._write_config(issue_tracking={"provider": "disabled"})
        self._set_task_state("l-x", "-evil")
        with patch.object(self.engine, "_current_branch") as cb_mock, \
                patch("optimize_completion.subprocess.run") as run_mock:
            result = self.engine._func_completion_dispatch(args={"completion_option": "keep"})
        self.assertFalse(result["success"])
        self.assertIn("Invalid feature branch name", result["error"])
        # The guard fires BEFORE any git op — including the branch probe.
        cb_mock.assert_not_called()
        run_mock.assert_not_called()


class TestEngineGitPushBranchValidation(_EngineBase):
    def test_git_push_rejects_invalid_task_state_branch(self):
        # _func_git_push is the provider-driven-completion argv sink reached
        # WITHOUT the dispatch precondition — it validates independently.
        self._set_task_state("l-x", "-evil")
        with patch("protocol_engine.subprocess.run") as run_mock:
            result = self.engine._func_git_push()
        self.assertFalse(result["success"])
        self.assertIn("Invalid branch name", result["error"])
        run_mock.assert_not_called()


# ---------------------------------------------------------------------------
# MCP layer still rejects via the shared helper
# ---------------------------------------------------------------------------

class MockMCP:
    def __init__(self):
        self.tools: Dict[str, Callable] = {}

    def tool(self):
        def decorator(func):
            self.tools[func.__name__] = func
            return func
        return decorator


class TestMCPBranchValidation(unittest.TestCase):
    def _git_push(self):
        from plugin.mcp.tools import git_operations as mcp_git
        m = MockMCP()
        mcp_git.register_tools(m)
        return mcp_git, m.tools["git_push"]

    def test_mcp_git_push_rejects_leading_dash(self):
        mcp_git, push = self._git_push()
        with patch.object(mcp_git, "get_project_root", return_value=Path("/proj")), \
                patch.object(mcp_git.subprocess, "run") as run_mock:
            result = push(branch="-D")
        self.assertFalse(result["success"])
        self.assertIn("Invalid branch name format", result["error"])
        run_mock.assert_not_called()

    def test_mcp_git_push_uses_shared_validator(self):
        # Drift guard: the tool module resolved the shared hooks validator.
        from plugin.mcp.tools import git_operations as mcp_git
        from git_operations import validate_branch_name as shared
        self.assertIs(mcp_git.validate_branch_name, shared)

    def test_code_review_utils_delegates(self):
        from plugin.mcp.helpers import code_review_utils as cru
        self.assertFalse(cru.validate_branch_name("-x"))
        self.assertFalse(cru.validate_branch_name("a;b"))
        self.assertTrue(cru.validate_branch_name("fix/x"))


if __name__ == "__main__":
    unittest.main()
