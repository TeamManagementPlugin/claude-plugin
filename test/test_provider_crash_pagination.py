"""Regression tests for h-fix-github-provider-crash-and-pagination.

Covers:
  C1  - branch-finders must not crash with AttributeError on a list response
        (github/pr_manager.find_pull_request_by_branch,
         gitlab/mr_manager.find_merge_request_by_branch).
  M1  - list endpoints paginate to collect all pages
        (gitlab/mr_manager.get_mr_notes, github/api._get_label_name_to_id_map).
  M10 - github/api.update_issue must not drop an empty-string title.
  R2-1- the configured auth token must be redacted from an error response body
        before it reaches a raised exception (base-class _redact_token).

Mirrors the MagicMock + __new__ construction pattern of
test/test_gitlab_mr_without_issue.py (no config.json / network needed).
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HOOKS_DIR = _REPO / "plugin" / "hooks"
for _p in (str(_HOOKS_DIR), str(_REPO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from github.pr_manager import GitHubPRManager  # noqa: E402
from github.api import GitHubAPI  # noqa: E402
from gitlab.mr_manager import GitLabMRManager  # noqa: E402
from gitlab.api import GitLabAPI  # noqa: E402


# --------------------------------------------------------------------------- C1

def _github_pr_manager(make_request_return):
    mgr = GitHubPRManager.__new__(GitHubPRManager)
    mgr.repository = "owner/repo"
    mgr.project_root = Path("/tmp")
    # A bare REAL api (only the network layer mocked) so the inherited
    # _raise_on_error actually runs — a full MagicMock would stub it away.
    api = GitHubAPI.__new__(GitHubAPI)
    api.api_token = None
    api._make_request = MagicMock(return_value=make_request_return)
    mgr.api = api
    return mgr


def _gitlab_mr_manager(make_request_return):
    mgr = GitLabMRManager.__new__(GitLabMRManager)
    mgr.project_path = "owner%2Frepo"
    mgr.project_root = Path("/tmp")
    mgr.default_branch = "main"
    mgr.default_labels = []
    # A bare REAL api (only the network layer mocked) so the inherited
    # _raise_on_error actually runs — a full MagicMock would stub it away.
    api = GitLabAPI.__new__(GitLabAPI)
    api.api_token = None
    api._make_request = MagicMock(return_value=make_request_return)
    mgr.api = api
    return mgr


def test_github_find_pr_by_branch_list_response_no_crash():
    """C1a: a non-empty list response (PRs exist) must not raise AttributeError."""
    pr = {"number": 7, "head": {"ref": "feature/x"}}
    mgr = _github_pr_manager([pr])
    result = mgr.find_pull_request_by_branch("feature/x")
    assert result == pr


def test_gitlab_find_mr_by_branch_list_response_no_crash():
    """C1b: a non-empty list response (MRs exist) must not raise AttributeError."""
    mr = {"iid": 3, "source_branch": "feature/x"}
    mgr = _gitlab_mr_manager([mr])
    result = mgr.find_merge_request_by_branch("feature/x")
    assert result == mr


def test_github_find_pr_by_branch_error_dict_still_raises():
    """An actual error dict must still raise (guard must not swallow errors)."""
    mgr = _github_pr_manager({"error": True, "status_code": 403, "message": "Forbidden"})
    with pytest.raises(Exception):
        mgr.find_pull_request_by_branch("feature/x")


def test_gitlab_find_mr_by_branch_error_dict_still_raises():
    mgr = _gitlab_mr_manager({"error": True, "status_code": 403, "message": "Forbidden"})
    with pytest.raises(Exception):
        mgr.find_merge_request_by_branch("feature/x")


def test_github_find_pr_by_branch_empty_list_returns_none():
    mgr = _github_pr_manager([])
    assert mgr.find_pull_request_by_branch("feature/x") is None


# --------------------------------------------------------------------------- M1

def _page_side_effect(pages):
    """Return a side_effect that serves pages[page-1] based on the &page= param."""
    def _se(method, endpoint, *args, **kwargs):
        page = 1
        for part in endpoint.replace("?", "&").split("&"):
            if part.startswith("page="):
                page = int(part.split("=", 1)[1])
        idx = page - 1
        return pages[idx] if 0 <= idx < len(pages) else []
    return _se


def test_gitlab_get_mr_notes_paginates_all_pages():
    """M1a: get_mr_notes must collect every page, not just the first 20/100."""
    page1 = [{"id": i} for i in range(100)]
    page2 = [{"id": 100 + i} for i in range(50)]  # short page -> stop
    mgr = _gitlab_mr_manager(None)
    mgr.api._make_request.side_effect = _page_side_effect([page1, page2])
    notes = mgr.get_mr_notes(5)
    assert len(notes) == 150
    assert mgr.api._make_request.call_count == 2


def test_github_label_map_paginates_all_pages():
    """M1b: _get_label_name_to_id_map must collect every page of labels."""
    page1 = [{"name": f"l{i}", "id": i} for i in range(100)]
    page2 = [{"name": f"l{100 + i}", "id": 100 + i} for i in range(5)]  # short -> stop
    api = GitHubAPI.__new__(GitHubAPI)
    api.repository = "owner/repo"
    api._label_cache = None
    api._make_request = MagicMock(side_effect=_page_side_effect([page1, page2]))
    label_map = api._get_label_name_to_id_map()
    assert len(label_map) == 105
    assert label_map["l104"] == 104
    assert api._make_request.call_count == 2


# -------------------------------------------------------------------------- M10

def test_github_update_issue_empty_title_not_dropped():
    """M10: an empty-string title must reach update_data (if title is not None)."""
    api = GitHubAPI.__new__(GitHubAPI)
    api.repository = "owner/repo"
    api.base_url = "https://api.github.com"
    api._make_request = MagicMock(return_value={})
    api.update_issue(1, title="")
    assert api._make_request.called
    sent_data = api._make_request.call_args[0][2]
    assert sent_data.get("title") == ""


# ------------------------------------------------------------------------- R2-1

def test_redact_token_replaces_configured_token():
    """R2-1: the configured api_token must be scrubbed from arbitrary text."""
    api = GitHubAPI.__new__(GitHubAPI)
    api.api_token = "ghp_supersecrettokenvalue"
    body = 'invalid token: ghp_supersecrettokenvalue (401)'
    redacted = api._redact_token(body)
    assert "ghp_supersecrettokenvalue" not in redacted
    assert "[REDACTED]" in redacted


def test_redact_token_no_token_is_noop():
    api = GitHubAPI.__new__(GitHubAPI)
    api.api_token = None
    assert api._redact_token("some body") == "some body"
    assert api._redact_token("") == ""
    assert api._redact_token(None) is None
