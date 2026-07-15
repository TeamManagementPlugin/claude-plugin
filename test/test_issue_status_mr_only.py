"""Regression tests for the `issue_status` MR/PR-only mapping KeyError.

Task: h-fix-gitlab-issue-status-keyerror

`issue_status` previously indexed task->issue mappings with bare subscripts on
issue-id keys: `mapping['gitlab_issue_id']` / `mapping['gitlab_issue_iid']` /
`mapping['last_synced']` (GitLab branch), `mapping['issue_id']` (GitHub branch),
and `mapping['last_synced']` (Jira branch). These raise `KeyError` on a mapping
that is MR-only (GitLab) or PR-only (GitHub) -- the shape written by
`create_merge_request_for_task` / `create_pull_request_from_task` when a task got
an MR/PR but no linked issue (e.g. `issue_tracking_enabled: false`).

The fix switches to `.get()` and skips non-issue-linked mappings from the
`tasks` array, preserving the documented contract that presence in `tasks` means
"issue-linked" (the task protocol's issue-linking validation).

Red-green: against the pre-fix code the gitlab/github MR/PR-only cases return
`success=False` (the tool catches the KeyError), so the `success is True`
assertions fail; with the fix they pass.
"""
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

from plugin.mcp.tools import issue_tracking  # noqa: E402


class MockMCP:
    """Minimal FastMCP stand-in that captures @mcp.tool()-decorated functions."""

    def __init__(self):
        self.tools: Dict[str, Callable] = {}

    def tool(self):
        def decorator(func):
            self.tools[func.__name__] = func
            return func

        return decorator


def _run_issue_status(provider: str, mappings: Dict[str, dict]) -> dict:
    """Register issue_tracking tools with patched provider getters and call issue_status."""
    mock_sync = MagicMock()
    mock_sync._load_mappings.return_value = mappings
    mock_api = MagicMock()  # attribute reads (base_url, etc.) return harmless sub-mocks

    with patch.object(issue_tracking, "detect_provider", return_value=provider), \
         patch.object(issue_tracking, "load_config", return_value={}), \
         patch.object(issue_tracking, "get_provider_sync", return_value=mock_sync), \
         patch.object(issue_tracking, "get_provider_api", return_value=mock_api):
        mcp = MockMCP()
        issue_tracking.register_tools(mcp)
        return mcp.tools["issue_status"]()


def _task_names(result: dict) -> list:
    return [t["task"] for t in result["tasks"]]


# --------------------------------------------------------------------------- #
# GitLab
# --------------------------------------------------------------------------- #

def test_gitlab_mr_only_mapping_no_keyerror():
    """MR-only GitLab mapping must not crash and must be skipped from `tasks`."""
    result = _run_issue_status("gitlab", {
        "m-mr-only": {
            "merge_request_id": 1,
            "merge_request_iid": 2,
            "merge_request_url": "https://gitlab.example.com/x/-/merge_requests/2",
            "last_synced": "2026-05-30T10:00:00",
        }
    })
    assert result["success"] is True
    assert result["provider"] == "GITLAB"
    assert _task_names(result) == []


def test_gitlab_issue_linked_mapping_present():
    """Issue-linked GitLab mapping still appears with the expected shape (regression)."""
    result = _run_issue_status("gitlab", {
        "h-linked": {
            "gitlab_issue_id": 42,
            "gitlab_issue_iid": 7,
            "last_synced": "2026-05-30T10:00:00",
        }
    })
    assert result["success"] is True
    assert len(result["tasks"]) == 1
    entry = result["tasks"][0]
    assert entry["task"] == "h-linked"
    # issue_status surfaces the project iid as the round-trip handle (not the
    # global id 42) -- see m-fix-issue-tracking-integration-robustness (B2).
    assert entry["issue_id"] == "gitlab:7"
    assert entry["issue_number"] == 7
    assert entry["last_synced"] == "2026-05-30T10:00:00"


def test_gitlab_mixed_mappings_only_linked_listed():
    """A mix of MR-only and issue-linked mappings lists only the issue-linked one."""
    result = _run_issue_status("gitlab", {
        "m-mr-only": {"merge_request_iid": 2, "last_synced": "2026-05-30T10:00:00"},
        "h-linked": {"gitlab_issue_id": 42, "gitlab_issue_iid": 7, "last_synced": "x"},
    })
    assert result["success"] is True
    assert _task_names(result) == ["h-linked"]


def test_linked_tasks_count_matches_listed_tasks():
    """linked_tasks must equal len(tasks) -- MR-only mappings are excluded from both."""
    result = _run_issue_status("gitlab", {
        "m-mr-only": {"merge_request_iid": 2, "last_synced": "x"},
        "h-linked": {"gitlab_issue_id": 42, "gitlab_issue_iid": 7, "last_synced": "x"},
    })
    assert result["success"] is True
    assert result["linked_tasks"] == len(result["tasks"]) == 1


def test_gitlab_linked_mapping_missing_last_synced_no_keyerror():
    """Issue-linked GitLab mapping without last_synced degrades to 'never'."""
    result = _run_issue_status("gitlab", {
        "h-linked": {"gitlab_issue_id": 42, "gitlab_issue_iid": 7},
    })
    assert result["success"] is True
    assert result["tasks"][0]["last_synced"] == "never"


# --------------------------------------------------------------------------- #
# GitHub
# --------------------------------------------------------------------------- #

def test_github_pr_only_mapping_no_keyerror():
    """PR-only GitHub mapping must not crash and must be skipped from `tasks`."""
    result = _run_issue_status("github", {
        "m-pr-only": {
            "pull_request_number": 7,
            "pull_request_url": "https://github.com/o/r/pull/7",
            "last_synced": "2026-05-30T10:00:00",
        }
    })
    assert result["success"] is True
    assert result["provider"] == "GITHUB"
    assert _task_names(result) == []


def test_github_issue_linked_mapping_present():
    """Issue-linked GitHub mapping still appears (regression)."""
    result = _run_issue_status("github", {
        "h-gh": {"issue_id": 99, "last_synced": "2026-05-30T10:00:00"},
    })
    assert result["success"] is True
    assert len(result["tasks"]) == 1
    entry = result["tasks"][0]
    assert entry["task"] == "h-gh"
    assert entry["issue_id"] == "github:99"
    assert entry["issue_number"] == 99


# --------------------------------------------------------------------------- #
# Jira (defensive only -- no MR/PR-only producer, but last_synced may be absent)
# --------------------------------------------------------------------------- #

def test_jira_mapping_missing_last_synced_no_keyerror():
    """Jira mapping without last_synced must not crash."""
    result = _run_issue_status("jira", {
        "h-j": {"jira_issue_id": "PROJ-1", "jira_issue_key": "PROJ-1"},
    })
    assert result["success"] is True
    assert result["provider"] == "JIRA"
    assert len(result["tasks"]) == 1
    assert result["tasks"][0]["issue_id"] == "jira:PROJ-1"
    assert result["tasks"][0]["last_synced"] == "never"
