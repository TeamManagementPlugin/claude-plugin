#!/usr/bin/env python3
"""m-enforcement-and-git-hardening — MCP git_operations hardening.

- git_commit must accept multi-line messages (Co-Authored-By trailers) — the old
  dangerous-char list rejected `\n`/`\r`/backtick/`$`, which is pointless under
  subprocess.run(shell=False). Only a NUL byte is now rejected.
- merge_request_create must default `target_branch` to the repo's detected
  default branch, not a hard-coded "master" (wrong on main-/develop-default
  repos). Covered via the new module-level `_detect_default_branch` helper.

Run with: python3 -m pytest test/test_git_operations_hardening.py -v
"""
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict
from unittest.mock import MagicMock, patch

_REPO = Path(__file__).resolve().parent.parent
_MCP_DIR = _REPO / "plugin" / "mcp"
_HOOKS_DIR = _REPO / "plugin" / "hooks"
for _p in (str(_REPO), str(_MCP_DIR), str(_HOOKS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from plugin.mcp.tools import git_operations  # noqa: E402


class MockMCP:
    """Minimal FastMCP stand-in capturing @mcp.tool()-decorated functions."""

    def __init__(self):
        self.tools: Dict[str, Callable] = {}

    def tool(self):
        def decorator(func):
            self.tools[func.__name__] = func
            return func
        return decorator


def _ok(stdout: str = "") -> MagicMock:
    return MagicMock(returncode=0, stdout=stdout, stderr="")


def _git_commit():
    m = MockMCP()
    git_operations.register_tools(m)
    return m.tools["git_commit"]


def _merge_request_create():
    m = MockMCP()
    git_operations.register_tools(m)
    return m.tools["merge_request_create"]


# ---------------------------------------------------------------------------
# git_commit — multi-line messages
# ---------------------------------------------------------------------------

class TestGitCommitMultiline:
    def test_multiline_message_with_trailer_succeeds(self):
        git_commit = _git_commit()
        msg = "feat: do the thing\n\nCo-Authored-By: Someone <s@example.com>"
        with patch.object(git_operations, "get_project_root", return_value=Path("/proj")), \
                patch.object(git_operations, "subprocess") as mock_sub:
            mock_sub.run.return_value = _ok()
            result = git_commit(message=msg)
        assert result["success"] is True, result
        # The multi-line message must reach `git commit -m <message>` verbatim.
        commit_calls = [c for c in mock_sub.run.call_args_list
                        if c.args[0][:2] == ["git", "commit"]]
        assert commit_calls, "git commit was never invoked"
        assert msg in commit_calls[0].args[0], "multi-line message not passed through"

    def test_nul_byte_rejected(self):
        git_commit = _git_commit()
        with patch.object(git_operations, "get_project_root", return_value=Path("/proj")), \
                patch.object(git_operations, "subprocess") as mock_sub:
            mock_sub.run.return_value = _ok()
            result = git_commit(message="bad\x00msg")
        assert result["success"] is False
        assert "NUL" in result["error"]
        # A rejected message must never reach git.
        assert not any(c.args[0][:2] == ["git", "commit"]
                       for c in mock_sub.run.call_args_list)

    def test_empty_message_still_rejected(self):
        git_commit = _git_commit()
        with patch.object(git_operations, "get_project_root", return_value=Path("/proj")):
            result = git_commit(message="   ")
        assert result["success"] is False
        assert "empty" in result["error"].lower()


# ---------------------------------------------------------------------------
# _detect_default_branch helper (module-level)
# ---------------------------------------------------------------------------

class TestDetectDefaultBranch:
    def test_prefers_origin_head(self):
        def run(cmd, *a, **kw):
            if cmd[:2] == ["git", "symbolic-ref"]:
                return MagicMock(returncode=0, stdout="origin/develop\n", stderr="")
            raise AssertionError(f"should not probe further: {cmd}")
        with patch.object(git_operations.subprocess, "run", side_effect=run):
            assert git_operations._detect_default_branch("/proj") == "develop"

    def test_falls_back_to_local_candidate(self):
        def run(cmd, *a, **kw):
            if cmd[:2] == ["git", "symbolic-ref"]:
                return MagicMock(returncode=1, stdout="", stderr="no ref")
            if cmd[:3] == ["git", "rev-parse", "--verify"]:
                # Only `develop` exists locally.
                rc = 0 if cmd[3] == "develop" else 1
                return MagicMock(returncode=rc, stdout="", stderr="")
            raise AssertionError(f"unexpected: {cmd}")
        with patch.object(git_operations.subprocess, "run", side_effect=run):
            assert git_operations._detect_default_branch("/proj") == "develop"

    def test_hardcoded_fallback_when_nothing_found(self):
        def run(cmd, *a, **kw):
            return MagicMock(returncode=1, stdout="", stderr="")
        with patch.object(git_operations.subprocess, "run", side_effect=run):
            assert git_operations._detect_default_branch("/proj") == "main"


# ---------------------------------------------------------------------------
# merge_request_create — target defaults to the detected branch
# ---------------------------------------------------------------------------

class TestMergeRequestCreateTargetDefault:
    def test_omitted_target_resolves_detected_default(self):
        """Calling with NO target_branch arg (codex test-gap: not target=None)
        must resolve the default branch via _detect_default_branch, proving the
        signature default is no longer the hard-coded 'master'."""
        mr_create = _merge_request_create()
        with patch.object(git_operations, "_detect_default_branch",
                          return_value="develop") as det, \
                patch.object(git_operations, "get_project_root",
                             return_value=Path("/proj")), \
                patch.object(git_operations, "load_config",
                             return_value={"gitlab": {"enabled": False}}):
            # target_branch omitted entirely → signature default None → detect.
            result = mr_create("m-x", "fix/x")
        det.assert_called_once()
        # gitlab disabled → early return, but the default was resolved first.
        assert result["success"] is False
        assert "GitLab is not configured" in result["error"]


# ---------------------------------------------------------------------------
# l-fix-subprocess-timeout-hardening — MCP git calls carry timeouts and handle
# subprocess.TimeoutExpired into a structured error instead of hanging.
#
# NOTE: these patch `git_operations.subprocess.run` specifically (NOT the whole
# `subprocess` module) so `subprocess.TimeoutExpired` stays the REAL exception
# class the new `except` clauses catch.
# ---------------------------------------------------------------------------

def _git_push():
    m = MockMCP()
    git_operations.register_tools(m)
    return m.tools["git_push"]


class TestGitTimeout:
    def test_push_timeout_returns_structured_error(self):
        push = _git_push()
        calls = []

        def run(cmd, *a, **kw):
            calls.append((list(cmd), kw))
            if cmd[:3] == ["git", "branch", "--show-current"]:
                return _ok("feature/x")
            if cmd[:2] == ["git", "push"]:
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
            return _ok()

        with patch.object(git_operations, "get_project_root", return_value=Path("/proj")), \
                patch.object(git_operations.subprocess, "run", side_effect=run):
            result = push()  # branch omitted → detect current branch, then push

        assert result["success"] is False
        assert "timed out" in result["error"].lower()
        # Both push-path calls must carry their intended bounds (not just push).
        branch_kw = next(kw for c, kw in calls if c[:3] == ["git", "branch", "--show-current"])
        push_kw = next(kw for c, kw in calls if c[:2] == ["git", "push"])
        assert branch_kw.get("timeout") == git_operations.GIT_TIMEOUT_FAST
        assert push_kw.get("timeout") == git_operations.GIT_TIMEOUT_SLOW

    def test_commit_timeout_returns_structured_error(self):
        git_commit = _git_commit()
        calls = []

        def run(cmd, *a, **kw):
            calls.append((list(cmd), kw))
            if cmd[:2] == ["git", "commit"]:
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
            return _ok()

        with patch.object(git_operations, "get_project_root", return_value=Path("/proj")), \
                patch.object(git_operations.subprocess, "run", side_effect=run):
            result = git_commit(message="feat: do the thing")

        assert result["success"] is False
        assert "timed out" in result["error"].lower()
        # Both commit-path calls must carry the MEDIUM bound.
        add_kw = next(kw for c, kw in calls if c[:2] == ["git", "add"])
        commit_kw = next(kw for c, kw in calls if c[:2] == ["git", "commit"])
        assert add_kw.get("timeout") == git_operations.GIT_TIMEOUT_MEDIUM
        assert commit_kw.get("timeout") == git_operations.GIT_TIMEOUT_MEDIUM
