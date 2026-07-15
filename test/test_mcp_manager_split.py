"""Regression tests for h-fix-mcp-manager-split-and-notebookedit (Defect 1).

After the provider monolith was split, MR/PR/release methods moved OUT of
`GitLabAPI`/`GitHubAPI` into manager classes (`GitLabMRManager`,
`GitLabReleaseManager`, `GitHubPRManager`, `GitHubReleaseManager`). The MCP
tools still called those methods on a bare `GitLabAPI()`/`GitHubAPI()`, so every
such call raised `AttributeError` — swallowed by the outer `except Exception`,
making the MR-comment / fetch-review / release tools silently non-functional.

These tests drive the ACTUAL MCP call path (tool function -> real manager
method) with the HTTP boundary (`_make_request`) mocked, asserting no
AttributeError leaks and a `success` result shape.

Hermeticity (per Codex review): a real `GitLabAPI()`/`GitHubAPI()` constructor
reads config.json, resolves tokens, and GitLab auto-detects the default branch
via `_make_request`. So we substitute a real-CLASS instance built via `__new__`
(no `__init__`) with the attributes the managers read set by hand and
`_make_request` mocked — exactly the pattern in test_provider_crash_pagination.
Because it is a real `GitLabAPI`/`GitHubAPI` instance (NOT a bare MagicMock), a
missing method genuinely raises AttributeError, so the test fails RED against the
pre-fix code instead of a MagicMock silently auto-creating the attribute.

Red-green: written RED against the pre-fix code; the manager-routing fix turns
each green.
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

import gitlab_utils  # noqa: E402  (top-level module — patch target for GitLabAPI)
import github_utils  # noqa: E402
from gitlab.api import GitLabAPI as RealGitLabAPI  # noqa: E402
from github.api import GitHubAPI as RealGitHubAPI  # noqa: E402
from plugin.mcp.tools import code_review, git_operations, release  # noqa: E402
from plugin.mcp.helpers import code_review_utils as cru  # noqa: E402


class MockMCP:
    """Minimal FastMCP stand-in that captures @mcp.tool()-decorated functions."""

    def __init__(self):
        self.tools: Dict[str, Callable] = {}

    def tool(self):
        def decorator(func):
            self.tools[func.__name__] = func
            return func

        return decorator


def _tool(module, name):
    mcp = MockMCP()
    module.register_tools(mcp)
    return mcp.tools[name]


def _gitlab_api(make_request: MagicMock) -> RealGitLabAPI:
    """A real GitLabAPI instance (no __init__) with manager-read attrs set."""
    api = RealGitLabAPI.__new__(RealGitLabAPI)
    api.project_root = Path("/tmp")
    api.project_path = "owner%2Frepo"
    api.default_branch = "main"
    api.default_labels = []
    api.base_url = "https://gitlab.example.com"
    api.raw_project_path = "owner/repo"
    api.api_base = "https://gitlab.example.com/api/v4"
    api.api_token = "tok"
    api._make_request = make_request
    return api


def _github_api(make_request: MagicMock) -> RealGitHubAPI:
    """A real GitHubAPI instance (no __init__) with manager-read attrs set."""
    api = RealGitHubAPI.__new__(RealGitHubAPI)
    api.project_root = Path("/tmp")
    api.repository = "owner/repo"
    api.api_token = "tok"
    api._make_request = make_request
    return api


def _release_router(release_dict):
    """GET (get_release existence probe) -> None; POST (create) -> release dict."""
    def route(method, endpoint, data=None):
        if method == "GET":
            return None
        return release_dict
    return MagicMock(side_effect=route)


def _no_attr_error(text) -> None:
    assert "has no attribute" not in str(text), f"AttributeError leaked: {text!r}"


# --------------------------------------------------------------------------- MR comment

def test_merge_request_comment_reaches_manager():
    api = _gitlab_api(MagicMock(return_value={"id": 42}))
    tool = _tool(code_review, "merge_request_comment")
    with patch.object(gitlab_utils, "GitLabAPI", return_value=api), \
         patch.object(code_review, "load_config", return_value={"gitlab": {"enabled": True}}), \
         patch.object(code_review, "setup_provider_imports"):
        result = tool(mr_iid=5, comment="hello")
    _no_attr_error(result.get("error", ""))
    assert result["success"] is True
    assert result["comment_added"] is True
    api._make_request.assert_called_once()


# --------------------------------------------------------------------------- find MR by branch

def test_code_review_find_mr_by_branch_reaches_manager():
    mr = {
        "iid": 7, "web_url": "http://x/mr/7", "source_branch": "feature/x",
        "title": "T", "description": "D", "target_branch": "main",
    }
    api = _gitlab_api(MagicMock(return_value=[mr]))
    tool = _tool(code_review, "code_review")
    with patch.object(gitlab_utils, "GitLabAPI", return_value=api), \
         patch.object(code_review, "detect_provider", return_value="gitlab"), \
         patch.object(code_review, "setup_provider_imports"):
        result = tool(branch="feature/x")
    for e in result.get("errors", []):
        _no_attr_error(e)
    assert result["mr_pr_found"] is True
    assert result["mr_pr_number"] == 7


def test_code_review_find_pr_by_branch_reaches_manager():
    pr = {
        "number": 11, "html_url": "http://x/pr/11",
        "head": {"ref": "feature/x"}, "base": {"ref": "main"},
        "title": "T", "body": "B",
    }
    api = _github_api(MagicMock(return_value=[pr]))
    tool = _tool(code_review, "code_review")
    with patch.object(github_utils, "GitHubAPI", return_value=api), \
         patch.object(code_review, "detect_provider", return_value="github"), \
         patch.object(code_review, "setup_provider_imports"):
        result = tool(branch="feature/x")
    for e in result.get("errors", []):
        _no_attr_error(e)
    assert result["mr_pr_found"] is True
    assert result["mr_pr_number"] == 11


# --------------------------------------------------------------------------- update MR

def test_merge_request_update_reaches_manager():
    api = _gitlab_api(MagicMock(return_value={"iid": 3, "web_url": "http://x/mr/3"}))
    tool = _tool(git_operations, "merge_request_update")
    with patch.object(gitlab_utils, "GitLabAPI", return_value=api), \
         patch.object(git_operations, "load_config", return_value={"gitlab": {"enabled": True}}), \
         patch.object(git_operations, "setup_provider_imports"):
        result = tool(mr_iid=3, title="New title")
    _no_attr_error(result.get("error", ""))
    assert result["success"] is True
    assert result["updated"] is True


def test_merge_request_create_reaches_manager():
    """16th broken site (found in code review): merge_request_create called the
    phantom method `create_merge_request_from_issue` on a bare GitLabAPI."""
    def route(method, endpoint, data=None):
        # get_issue (GET) -> issue dict; MR create (POST) -> MR dict
        return {"title": "Iss", "labels": []} if method == "GET" \
            else {"iid": 5, "web_url": "http://x/mr/5"}
    api = _gitlab_api(MagicMock(side_effect=route))
    fake_sync = MagicMock()
    fake_sync.gitlab = api
    fake_sync.get_task_mapping.return_value = {"gitlab_issue_iid": 42}
    tool = _tool(git_operations, "merge_request_create")
    with patch.object(gitlab_utils, "GitLabTaskSync", return_value=fake_sync), \
         patch("gitlab.mr_manager.get_commit_log", return_value=[]), \
         patch.object(git_operations, "load_config", return_value={"gitlab": {"enabled": True}}), \
         patch.object(git_operations, "setup_provider_imports"):
        result = tool(task_name="t-x", source_branch="feature/x", target_branch="main")
    _no_attr_error(result.get("error", ""))
    assert result["success"] is True
    assert result["merge_request_iid"] == 5


# --------------------------------------------------------------------------- release create

def test_release_create_gitlab_reaches_manager():
    api = _gitlab_api(_release_router(
        {"tag_name": "v1.0.0", "_links": {"self": "http://x"}, "assets": {"links": []}}
    ))
    tool = _tool(release, "release_create")
    # The tag create/push subprocess calls were extracted into the shared
    # git_operations.ensure_tag_created_and_pushed helper, so mock there.
    with patch.object(gitlab_utils, "GitLabAPI", return_value=api), \
         patch("git_operations.subprocess"), \
         patch.object(release, "load_config",
                      return_value={"gitlab": {"enabled": True}, "github": {"enabled": False}}), \
         patch.object(release, "setup_provider_imports"):
        result = tool(tag_name="v1.0.0", release_name="R", description="notes")
    _no_attr_error(result.get("errors", []))
    assert result["success"] is True
    assert result["gitlab"]["success"] is True


def test_release_create_github_reaches_manager():
    api = _github_api(_release_router(
        {"tag_name": "v1.0.0", "html_url": "http://x", "id": 5}
    ))
    tool = _tool(release, "release_create")
    # Tag subprocess now lives in the shared git_operations helper (see above).
    with patch.object(github_utils, "GitHubAPI", return_value=api), \
         patch("git_operations.subprocess"), \
         patch.object(release, "load_config",
                      return_value={"gitlab": {"enabled": False}, "github": {"enabled": True}}), \
         patch.object(release, "setup_provider_imports"):
        result = tool(tag_name="v1.0.0", release_name="R", description="notes")
    _no_attr_error(result.get("errors", []))
    assert result["success"] is True
    assert result["github"]["success"] is True


# --------------------------------------------------------------------------- helper: fetch by URL

def test_fetch_mr_pr_by_url_gitlab_reaches_manager():
    """The 15th broken site the audit missed: helpers/code_review_utils.py:144."""
    api = _gitlab_api(MagicMock(return_value={"iid": 9, "source_branch": "feature/x", "web_url": "http://x"}))
    with patch.object(gitlab_utils, "GitLabAPI", return_value=api), \
         patch.object(cru, "setup_provider_imports"):
        mr = cru.fetch_mr_pr_by_url({"provider": "gitlab", "id": "9", "url": "http://x"})
    assert mr is not None
    assert mr["iid"] == 9
