"""Regression tests for GitLab consumer hardening against MR-only mappings.

Task: h-fix-gitlab-issue-status-keyerror (scope A -- consumer-hardening parity
with the GitHub fix in m-fix-github-pr-without-issue).

`create_merge_request_for_task` (gitlab/task_sync.py:341-393) writes an MR-only
mapping (`merge_request_*` + `last_synced`, no `gitlab_issue_id` / `gitlab_issue_iid`)
when a task gets a standalone merge request with no linked issue. Three consumers
previously did a bare `mapping['gitlab_issue_iid']`, which `KeyError`s on that shape:

  - GitLabTaskSync.update_issue_description_from_task  (raise -> reachable via MCP issue_push)
  - GitLabTaskSync.sync_task_status_to_gitlab          (no-op -> reachable via MCP issue_sync / auto-sync)
  - merge_request_create MCP tool (git_operations.py)  (return success=False)

This mirrors test/test_github_pr_without_issue.py.
"""
import sys
from pathlib import Path
from typing import Callable, Dict
from unittest.mock import MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parent.parent
_MCP_DIR = _REPO / "plugin" / "mcp"
_HOOKS_DIR = _REPO / "plugin" / "hooks"
for _p in (str(_HOOKS_DIR), str(_MCP_DIR), str(_REPO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from gitlab_utils import GitLabTaskSync  # noqa: E402  (re-exported from gitlab.task_sync)

# Module object the class actually lives in, for patching find_task_file robustly.
_TASK_SYNC_MOD = sys.modules[GitLabTaskSync.__module__]

TASK_BODY = "---\ntask: m-foo\n---\n\n# Foo Title\n\nBody.\n"

MR_ONLY_MAPPING = {
    "merge_request_id": 1,
    "merge_request_iid": 2,
    "merge_request_url": "https://gitlab.example.com/x/-/merge_requests/2",
    "last_synced": "2026-05-30T10:00:00",
}
LINKED_MAPPING = {
    "gitlab_issue_id": 42,
    "gitlab_issue_iid": 7,
    "last_synced": "2026-05-30T10:00:00",
}


@pytest.fixture
def sync(tmp_path):
    """GitLabTaskSync instance with stubbed API + mappings store."""
    s = GitLabTaskSync.__new__(GitLabTaskSync)
    s.project_root = tmp_path
    s.gitlab = MagicMock()
    s._mappings_store = {}
    s._load_mappings = lambda: dict(s._mappings_store)

    def _save(mappings):
        s._mappings_store = dict(mappings)

    s._save_mappings = _save
    return s


@pytest.fixture
def task_file(tmp_path):
    tasks_dir = tmp_path / "team-management" / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    f = tasks_dir / "m-foo.md"
    f.write_text(TASK_BODY, encoding="utf-8")
    return f


# --------------------------------------------------------------------------- #
# update_issue_description_from_task
# --------------------------------------------------------------------------- #

def test_update_description_mr_only_raises_not_linked(sync, task_file):
    """MR-only mapping -> 'not linked to GitLab issue' exception, not KeyError.

    The task file exists (via the fixture), so pre-fix this path reaches the bare
    `mapping['gitlab_issue_iid']` and raises KeyError; the guard must fire first.
    """
    sync.get_task_mapping = lambda task_name: dict(MR_ONLY_MAPPING)
    with patch.object(_TASK_SYNC_MOD, "find_task_file", return_value=task_file):
        with pytest.raises(Exception, match="not linked to GitLab issue"):
            sync.update_issue_description_from_task("m-foo")
    sync.gitlab.update_issue.assert_not_called()


def test_update_description_no_mapping_raises_not_linked(sync):
    """Absent mapping -> same 'not linked' exception (regression for the original guard)."""
    sync.get_task_mapping = lambda task_name: None
    with pytest.raises(Exception, match="not linked to GitLab issue"):
        sync.update_issue_description_from_task("m-foo")


def test_update_description_issue_linked_updates(sync, task_file):
    """Issue-linked mapping -> the GitLab issue is updated (regression)."""
    sync.get_task_mapping = lambda task_name: dict(LINKED_MAPPING)
    sync.gitlab.update_issue.return_value = {"id": 42, "iid": 7}
    with patch.object(_TASK_SYNC_MOD, "find_task_file", return_value=task_file):
        result = sync.update_issue_description_from_task("m-foo")
    assert result is True
    sync.gitlab.update_issue.assert_called_once()
    assert sync.gitlab.update_issue.call_args.args[0] == 7  # issue_iid


# --------------------------------------------------------------------------- #
# sync_task_status_to_gitlab
# --------------------------------------------------------------------------- #

def test_sync_status_mr_only_is_noop(sync):
    """MR-only mapping -> silent no-op, no KeyError, no API calls."""
    sync.get_task_mapping = lambda task_name: dict(MR_ONLY_MAPPING)
    sync.sync_task_status_to_gitlab("m-foo", "in-progress")  # must not raise
    sync.gitlab.add_issue_comment.assert_not_called()
    sync.gitlab.update_issue.assert_not_called()


def test_sync_status_no_mapping_is_noop(sync):
    """Absent mapping -> no-op (regression for the original guard)."""
    sync.get_task_mapping = lambda task_name: None
    sync.sync_task_status_to_gitlab("m-foo", "in-progress")
    sync.gitlab.add_issue_comment.assert_not_called()


def test_sync_status_issue_linked_comments(sync, task_file):
    """Issue-linked mapping -> the linked issue receives a status comment (regression)."""
    sync.get_task_mapping = lambda task_name: dict(LINKED_MAPPING)
    sync.gitlab.update_issue.return_value = {"id": 42, "iid": 7}
    with patch.object(_TASK_SYNC_MOD, "find_task_file", return_value=task_file):
        sync.sync_task_status_to_gitlab("m-foo", "in-progress")
    sync.gitlab.add_issue_comment.assert_called_once()
    assert sync.gitlab.add_issue_comment.call_args.args[0] == 7  # issue_iid


# --------------------------------------------------------------------------- #
# MCP issue_push wrapper round-trip (codex test-gap: wrapper must not crash)
# --------------------------------------------------------------------------- #

class _MockMCP:
    def __init__(self):
        self.tools: Dict[str, Callable] = {}

    def tool(self):
        def decorator(func):
            self.tools[func.__name__] = func
            return func

        return decorator


def test_issue_push_mr_only_returns_structured_error(sync, task_file):
    """issue_push -> update_issue_description_from_task on an MR-only mapping
    must surface a structured 'not linked' error, not raise/KeyError."""
    from plugin.mcp.tools import issue_tracking

    sync.get_task_mapping = lambda task_name: dict(MR_ONLY_MAPPING)

    with patch.object(issue_tracking, "detect_provider", return_value="gitlab"), \
         patch.object(issue_tracking, "get_provider_sync", return_value=sync), \
         patch.object(issue_tracking, "find_task_file", return_value=task_file):
        mcp = _MockMCP()
        issue_tracking.register_tools(mcp)
        result = mcp.tools["issue_push"]("m-foo")

    assert result["success"] is False
    assert "not linked to GitLab issue" in result["error"]


# --------------------------------------------------------------------------- #
# merge_request_create MCP tool
# --------------------------------------------------------------------------- #

def _run_merge_request_create(mapping):
    """Register git_operations tools with a stubbed GitLabTaskSync and call merge_request_create."""
    from plugin.mcp.tools import git_operations

    mock_sync = MagicMock()
    mock_sync.get_task_mapping.return_value = mapping
    # MR creation now routes through GitLabMRManager(sync.gitlab).create_merge_request
    # (the old sync.gitlab.create_merge_request_from_issue was a phantom method that
    # never existed on GitLabAPI — this test previously passed vacuously against a
    # MagicMock). Stub the manager's create_merge_request.
    mock_mgr = MagicMock()
    mock_mgr.create_merge_request.return_value = {
        "iid": 5,
        "web_url": "https://gitlab.example.com/x/-/merge_requests/5",
    }

    with patch.object(git_operations, "load_config", return_value={"gitlab": {"enabled": True}}), \
         patch.object(git_operations, "setup_provider_imports", MagicMock()), \
         patch("gitlab_utils.GitLabTaskSync", return_value=mock_sync), \
         patch("gitlab_utils.GitLabMRManager", return_value=mock_mgr):
        mcp = _MockMCP()
        git_operations.register_tools(mcp)
        return mcp.tools["merge_request_create"]("m-foo", "fix/foo", "main")


def test_merge_request_create_mr_only_returns_not_linked():
    """MR-only mapping -> success=False 'not linked', not KeyError."""
    result = _run_merge_request_create(dict(MR_ONLY_MAPPING))
    assert result["success"] is False
    assert "not linked to GitLab issue" in result["error"]


def test_merge_request_create_issue_linked_succeeds():
    """Issue-linked mapping -> MR creation proceeds past the guard (regression)."""
    result = _run_merge_request_create(dict(LINKED_MAPPING))
    assert result["success"] is True
    assert result["merge_request_iid"] == 5
