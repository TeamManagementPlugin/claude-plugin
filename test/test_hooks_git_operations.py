#!/usr/bin/env python3
"""Behavioral tests for the HOOKS-layer git helper `plugin/hooks/git_operations.py`.

This is the provider-shared git wrapper (`run_git`), NOT the same-named MCP tool
module `plugin/mcp/tools/git_operations.py`. The two are distinct: the hooks one
is a top-level module (`import git_operations`), the MCP one is package-qualified
(`tools.git_operations`), so there is no import collision even when both dirs are
on sys.path in the same pytest session.

`run_git(args, description, cwd)` contract (git_operations.py:14-43):
  * a git command that RUNS returns its `CompletedProcess` verbatim — success
    (rc 0) AND failure (rc != 0) both return the process; run_git does NOT inspect
    returncode.
  * `subprocess.TimeoutExpired` -> None (with a stderr note).
  * any other Exception (e.g. OSError, git-not-found) -> None.
  * the timeout is `int(os.getenv("BCC_GIT_TIMEOUT_SECONDS", "30"))` — env override,
    default 30. Mirrors the BCC_HTTP_TIMEOUT_SECONDS override tested in
    test_provider_make_request.py:163.

Run with: python3 -m pytest test/test_hooks_git_operations.py -v
"""
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "plugin" / "hooks"
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

import git_operations  # noqa: E402  (top-level hooks module, not tools.git_operations)

# Guard: this MUST be the HOOKS git_operations (plugin/hooks/git_operations.py),
# not the same-named MCP tool module (tools.git_operations) or a deployed copy —
# a prior test's sys.path/sys.modules state could otherwise shadow it (codex).
assert Path(git_operations.__file__).resolve() == (HOOKS_DIR / "git_operations.py"), \
    f"imported the wrong git_operations: {git_operations.__file__}"


def _fake_completed(returncode=0):
    m = MagicMock(spec=subprocess.CompletedProcess)
    m.returncode = returncode
    m.stdout = ""
    m.stderr = ""
    return m


# --------------------------------------------------------------- runs -> process

def test_success_returns_completed_process(tmp_path):
    """A git command that runs and exits 0 returns its CompletedProcess."""
    result = git_operations.run_git(["git", "--version"], "version", cwd=tmp_path)
    assert result is not None
    assert result.returncode == 0


def test_failure_returns_nonzero_process(tmp_path):
    """A git command that exits non-zero (rev-parse in a non-repo) returns the
    CompletedProcess with a non-zero returncode — NOT None. run_git only returns
    None for exceptions/timeouts, never for a clean non-zero exit."""
    result = git_operations.run_git(
        ["git", "rev-parse", "--verify", "HEAD"], "rev-parse", cwd=tmp_path)
    assert result is not None
    assert result.returncode != 0


# ------------------------------------------------------- exception/timeout -> None

def test_timeout_returns_none(tmp_path, monkeypatch):
    """subprocess.TimeoutExpired -> None."""
    monkeypatch.setattr(
        git_operations.subprocess, "run",
        MagicMock(side_effect=subprocess.TimeoutExpired(cmd="git", timeout=1)))
    result = git_operations.run_git(["git", "status"], "status", cwd=tmp_path)
    assert result is None


def test_generic_exception_returns_none(tmp_path, monkeypatch):
    """Any other Exception (e.g. git binary missing -> OSError) -> None."""
    monkeypatch.setattr(
        git_operations.subprocess, "run",
        MagicMock(side_effect=OSError("git not found")))
    result = git_operations.run_git(["git", "status"], "status", cwd=tmp_path)
    assert result is None


# --------------------------------------------------- BCC_GIT_TIMEOUT_SECONDS override

def test_env_timeout_override(tmp_path, monkeypatch):
    """BCC_GIT_TIMEOUT_SECONDS overrides the default 30s timeout passed to
    subprocess.run (mirrors test_provider_make_request.py:163's BCC pattern)."""
    monkeypatch.setenv("BCC_GIT_TIMEOUT_SECONDS", "7")
    mock_run = MagicMock(return_value=_fake_completed(0))
    monkeypatch.setattr(git_operations.subprocess, "run", mock_run)

    git_operations.run_git(["git", "status"], "status", cwd=tmp_path)
    assert mock_run.call_args.kwargs["timeout"] == 7


def test_default_timeout_when_env_unset(tmp_path, monkeypatch):
    """Unset BCC_GIT_TIMEOUT_SECONDS -> default 30s."""
    monkeypatch.delenv("BCC_GIT_TIMEOUT_SECONDS", raising=False)
    mock_run = MagicMock(return_value=_fake_completed(0))
    monkeypatch.setattr(git_operations.subprocess, "run", mock_run)

    git_operations.run_git(["git", "status"], "status", cwd=tmp_path)
    assert mock_run.call_args.kwargs["timeout"] == 30


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
