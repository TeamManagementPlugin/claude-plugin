"""Regression tests for MCP git/code-review tooling correctness.

Task: m-fix-mcp-git-review-tooling (deep-audit round 2: R2-7 .. R2-12)

Findings covered:
- R2-7  Branch-name validation rejects valid dotted names (release/1.0.0) at TWO
        sites -- git_push (mcp/tools/git_operations.py) and validate_branch_name
        (mcp/helpers/code_review_utils.py) -- and neither site rejects a leading
        '-' today ('-' inside the character class matches at any position), so
        '-foo' reaches `git push -u origin -foo` as an option-injection path.
- R2-8  restore_git_environment reports success=True even when the stash pop
        failed (success only mirrored the branch checkout) -- silent data loss.
- R2-9  No timeout on any restore-side git call (checkout / stash list /
        stash pop); a hung pop blocks forever with no actionable error.
- R2-10 Bare `git stash pop` pops index 0 after matching the stash *message*
        in `git stash list` -- an interloper stash pushed between prepare and
        restore gets popped instead of the review stash.
- R2-11 detect_provider returns the cached _provider BEFORE load_config(), so
        mtime-based config invalidation never runs once the cache is set -- a
        provider change is invisible until MCP server restart.
- R2-12 parse_mr_pr_url GitHub patterns use unanchored re.search, so
        https://evil.com/forward/github.com/owner/repo/pull/123 mis-parses as
        github.com. Anchoring is an intentional contract change: scheme-less
        URLs stop parsing (provider web_url fields always carry a scheme).

Red-green: written RED against the pre-fix code; each fix turns its group green.
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict
from unittest.mock import MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parent.parent
_MCP_DIR = _REPO / "plugin" / "mcp"
_HOOKS_DIR = _REPO / "plugin" / "hooks"
for _p in (str(_REPO), str(_MCP_DIR), str(_HOOKS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from helpers import code_review_utils as cru  # noqa: E402
from core import config as core_config  # noqa: E402
from plugin.mcp.tools import git_operations  # noqa: E402


class MockMCP:
    """Minimal FastMCP stand-in that captures @mcp.tool()-decorated functions."""

    def __init__(self):
        self.tools: Dict[str, Callable] = {}

    def tool(self):
        def decorator(func):
            self.tools[func.__name__] = func
            return func

        return decorator


def _ok(stdout: str = "") -> MagicMock:
    return MagicMock(returncode=0, stdout=stdout, stderr="")


# ---------------------------------------------------------------------------
# R2-7: branch-name validation (both sites)
# ---------------------------------------------------------------------------

class TestValidateBranchName:
    def test_accepts_dotted_branch(self):
        assert cru.validate_branch_name("release/1.0.0") is True
        assert cru.validate_branch_name("fix/issue-2.4") is True

    def test_rejects_leading_dash(self):
        assert cru.validate_branch_name("-foo") is False
        assert cru.validate_branch_name("--force") is False

    def test_still_rejects_unsafe_characters(self):
        assert cru.validate_branch_name("foo;rm -rf") is False
        assert cru.validate_branch_name("foo$(x)") is False
        assert cru.validate_branch_name("foo bar") is False


class TestGitPushBranchValidation:
    def _git_push(self):
        mock_mcp = MockMCP()
        git_operations.register_tools(mock_mcp)
        return mock_mcp.tools["git_push"]

    def test_accepts_dotted_branch(self):
        git_push = self._git_push()
        with patch.object(git_operations, "subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = _ok()
            result = git_push(branch="release/1.0.0")
        assert result["success"] is True
        assert result["branch"] == "release/1.0.0"

    def test_rejects_leading_dash(self):
        git_push = self._git_push()
        with patch.object(git_operations, "subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = _ok()
            result = git_push(branch="-foo")
        assert result["success"] is False
        assert "Invalid branch name" in result["error"]
        # The push itself must never have been attempted.
        for call in mock_subprocess.run.call_args_list:
            assert call.args[0][:2] != ["git", "push"]


# ---------------------------------------------------------------------------
# R2-8 / R2-9 / R2-10: restore_git_environment
# ---------------------------------------------------------------------------

_MARKER = "Temporary stash for code review"
_SINGLE_STASH = f"stash@{{0}}: On fix/x: {_MARKER}\n"
_INTERLOPER_STASH = (
    "stash@{0}: WIP on other: interloper\n"
    f"stash@{{1}}: On fix/x: {_MARKER}\n"
)


def _restore_router(stash_list_stdout: str, pop_behavior=None):
    """Return (side_effect, calls) routing git subcommands in restore."""
    calls = []

    def run(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        if cmd[:2] == ["git", "checkout"]:
            return _ok()
        if cmd[:3] == ["git", "stash", "list"]:
            return _ok(stash_list_stdout)
        if cmd[:3] == ["git", "stash", "pop"]:
            if isinstance(pop_behavior, BaseException):
                raise pop_behavior
            return _ok()
        return _ok()

    return run, calls


class TestRestoreGitEnvironment:
    def _run(self, stash_list_stdout: str, pop_behavior=None, was_stashed=True):
        side_effect, calls = _restore_router(stash_list_stdout, pop_behavior)
        with patch.object(cru, "get_project_root", return_value=_REPO), \
             patch.object(cru.subprocess, "run", side_effect=side_effect):
            result = cru.restore_git_environment("main", was_stashed)
        return result, calls

    # --- R2-8 ---

    def test_pop_failure_returns_success_false(self):
        err = subprocess.CalledProcessError(
            1, ["git", "stash", "pop"], stderr="CONFLICT (content): merge conflict"
        )
        result, _ = self._run(_SINGLE_STASH, pop_behavior=err)
        assert result["restored"] is True
        assert result["unstashed"] is False
        assert result["success"] is False
        assert result["errors"]

    def test_stash_not_found_returns_success_false(self):
        result, _ = self._run("stash@{0}: WIP on other: unrelated\n")
        assert result["restored"] is True
        assert result["unstashed"] is False
        assert result["success"] is False

    def test_no_stash_taken_still_succeeds(self):
        result, _ = self._run("", was_stashed=False)
        assert result["restored"] is True
        assert result["success"] is True

    # --- R2-9 ---

    def test_pop_timeout_surfaces_actionable_error(self):
        err = subprocess.TimeoutExpired(cmd="git stash pop", timeout=30)
        result, _ = self._run(_SINGLE_STASH, pop_behavior=err)
        assert result["success"] is False
        assert any("manual" in e.lower() for e in result["errors"])

    def test_all_restore_git_calls_have_timeout(self):
        _, calls = self._run(_SINGLE_STASH)
        assert calls, "expected git calls to be recorded"
        for cmd, kwargs in calls:
            assert "timeout" in kwargs, f"git call without timeout: {cmd}"

    # --- R2-10 ---

    def test_pop_targets_exact_ref_with_interloper(self):
        result, calls = self._run(_INTERLOPER_STASH)
        pop_calls = [c for c, _ in calls if c[:3] == ["git", "stash", "pop"]]
        assert pop_calls, "expected a stash pop call"
        assert pop_calls[0] == ["git", "stash", "pop", "stash@{1}"]
        assert result["unstashed"] is True
        assert result["success"] is True

    def test_pop_targets_exact_ref_single_stash(self):
        result, calls = self._run(_SINGLE_STASH)
        pop_calls = [c for c, _ in calls if c[:3] == ["git", "stash", "pop"]]
        assert pop_calls[0] == ["git", "stash", "pop", "stash@{0}"]
        assert result["success"] is True

    # --- existing-behaviour guard ---

    def test_checkout_failure_returns_success_false(self):
        def side_effect(cmd, **kwargs):
            if cmd[:2] == ["git", "checkout"]:
                raise subprocess.CalledProcessError(1, cmd, stderr="boom")
            return _ok()

        with patch.object(cru, "get_project_root", return_value=_REPO), \
             patch.object(cru.subprocess, "run", side_effect=side_effect):
            result = cru.restore_git_environment("main", True)
        assert result["success"] is False
        assert result["restored"] is False


# ---------------------------------------------------------------------------
# l-fix-subprocess-timeout-hardening: prepare_review_environment
# The 3 LOCAL ops (branch/status/stash) gained timeouts; the function gained a
# dedicated TimeoutExpired handler so a hang yields a clear "timed out" error
# instead of the generic "Unexpected error".
# ---------------------------------------------------------------------------

def _prepare_router(status_stdout=" M file.py\n", timeout_on=None):
    """Route prepare_review_environment's git subcommands; optionally raise
    TimeoutExpired on the command whose prefix matches `timeout_on`."""
    calls = []

    def run(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        if timeout_on and cmd[: len(timeout_on)] == timeout_on:
            raise subprocess.TimeoutExpired(cmd=" ".join(cmd), timeout=kwargs.get("timeout"))
        if cmd[:3] == ["git", "branch", "--show-current"]:
            return _ok("fix/current")  # differs from target → full prepare path
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return _ok(status_stdout)
        return _ok()

    return run, calls


class TestPrepareGitEnvironment:
    def _run(self, target="fix/target", status_stdout=" M file.py\n", timeout_on=None):
        side_effect, calls = _prepare_router(status_stdout, timeout_on)
        with patch.object(cru, "get_project_root", return_value=_REPO), \
             patch.object(cru.subprocess, "run", side_effect=side_effect):
            result = cru.prepare_review_environment(target)
        return result, calls

    def test_all_prepare_git_calls_have_timeout(self):
        result, calls = self._run()
        assert result["success"] is True  # branch/status/stash/fetch/checkout/pull
        assert calls, "expected git calls to be recorded"
        for cmd, kwargs in calls:
            assert "timeout" in kwargs, f"git call without timeout: {cmd}"

    def test_local_op_timeout_surfaces_dedicated_error(self):
        # A hang on a LOCAL op (status) now yields a clear timeout error rather
        # than the generic "Unexpected error".
        result, _ = self._run(timeout_on=["git", "status", "--porcelain"])
        assert result["success"] is False
        assert any("timed out" in e.lower() for e in result["errors"])
        assert not any("Unexpected error" in e for e in result["errors"])

    def test_network_op_timeout_surfaces_dedicated_error(self):
        result, _ = self._run(timeout_on=["git", "fetch"])
        assert result["success"] is False
        assert any("timed out" in e.lower() for e in result["errors"])


# ---------------------------------------------------------------------------
# R2-11: detect_provider config reload
# ---------------------------------------------------------------------------

class TestDetectProviderReload:
    @pytest.fixture(autouse=True)
    def _reset_config_cache(self):
        core_config.reload_config()
        yield
        core_config.reload_config()

    def _write_config(self, cfg_file: Path, provider: str) -> None:
        cfg_file.write_text(
            json.dumps(
                {
                    "issue_tracking": {"provider": provider},
                    provider: {"enabled": True},
                }
            ),
            encoding="utf-8",
        )

    def test_provider_redetected_after_config_change(self, tmp_path, monkeypatch):
        cfg_dir = tmp_path / "team-management"
        cfg_dir.mkdir()
        cfg_file = cfg_dir / "config.json"

        self._write_config(cfg_file, "gitlab")
        monkeypatch.setattr(core_config, "get_project_root", lambda: tmp_path)

        assert core_config.detect_provider() == "gitlab"

        self._write_config(cfg_file, "github")
        # Guarantee an mtime change even on coarse-grained filesystems.
        stat = cfg_file.stat()
        os.utime(cfg_file, (stat.st_atime + 10, stat.st_mtime + 10))

        assert core_config.detect_provider() == "github"


# ---------------------------------------------------------------------------
# R2-12: parse_mr_pr_url anchoring
# ---------------------------------------------------------------------------

class TestParseMrPrUrl:
    def test_forwarded_github_url_not_misparsed(self):
        result = cru.parse_mr_pr_url(
            "https://evil.com/forward/github.com/owner/repo/pull/123"
        )
        # Must NOT be treated as public github.com. Either no parse at all,
        # or an enterprise parse whose host is evil.com -- never api.github.com.
        if result is not None:
            assert result["base_url"] != "https://api.github.com"

    def test_public_github_url_parses(self):
        result = cru.parse_mr_pr_url("https://github.com/owner/repo/pull/5")
        assert result == {
            "provider": "github",
            "id": "5",
            "base_url": "https://api.github.com",
            "repository": "owner/repo",
            "url": "https://github.com/owner/repo/pull/5",
        }

    def test_enterprise_github_url_parses(self):
        result = cru.parse_mr_pr_url("https://ghe.corp.example/owner/repo/pull/7")
        assert result is not None
        assert result["provider"] == "github"
        assert result["id"] == "7"
        assert result["base_url"] == "https://ghe.corp.example/api/v3"
        assert result["repository"] == "owner/repo"

    def test_gitlab_url_parses(self):
        result = cru.parse_mr_pr_url(
            "https://gitlab.com/group/project/-/merge_requests/42"
        )
        assert result is not None
        assert result["provider"] == "gitlab"
        assert result["id"] == "42"

    def test_schemeless_github_url_returns_none(self):
        # Intentional contract change: provider web_url fields always carry a
        # scheme; scheme-less strings no longer parse.
        assert cru.parse_mr_pr_url("github.com/owner/repo/pull/5") is None
