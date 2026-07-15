"""Tests for create_pull_request_from_task standalone (no-issue) flow.

Bug: when github.issue_tracking_enabled is false, no task->issue mapping is
created, but create_pull_request_from_task hard-fails on missing mapping.
The documented contract (CLAUDE.md:249) says the flag does not affect PR
creation. This regression suite mirrors the GitLab dual-path pattern at
plugin/hooks/gitlab/task_sync.py:341-379.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

HOOKS_DIR = Path(__file__).resolve().parent.parent / "plugin" / "hooks"
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

from github.task_sync import GitHubTaskSync  # noqa: E402


TASK_BODY = "---\ntask: m-foo\n---\n\n# Foo Title\n\nBody.\n"


@pytest.fixture
def sync(tmp_path):
    """GitHubTaskSync instance with stubbed API + mappings store."""
    s = GitHubTaskSync.__new__(GitHubTaskSync)
    s.project_root = tmp_path
    s.github = MagicMock()
    s.github.get_default_branch.return_value = "main"
    s.github.project_root = tmp_path
    s.github.repository = "owner/repo"

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


def _captured_create_pull_request(sync, task_file_path):
    """Patch GitHubPRManager.create_pull_request to capture call args.

    Returns (captured_kwargs_dict, fake_pr_response).
    """
    captured = {}
    fake_pr = {"number": 7, "html_url": "https://github.com/owner/repo/pull/7"}

    def fake_create(self, title, body, head, base=None):
        captured["title"] = title
        captured["body"] = body
        captured["head"] = head
        captured["base"] = base
        return fake_pr

    return captured, fake_pr, fake_create


def test_create_pr_without_mapping(sync, task_file):
    """No issue mapping -> standalone PR is created, body lacks 'Closes #'."""
    sync.get_task_mapping = lambda task_name: None

    captured, fake_pr, fake_create = _captured_create_pull_request(sync, task_file)

    with patch(
        "github.pr_manager.GitHubPRManager.create_pull_request",
        new=fake_create,
    ):
        with patch("github.task_sync.find_task_file", return_value=task_file):
            result = sync.create_pull_request_from_task(
                "m-foo", "fix/foo", target_branch="main"
            )

    assert result == fake_pr, "Expected the PR dict to be returned"
    assert captured["title"] == "Resolve: Foo Title"
    assert "Closes #" not in captured["body"], (
        "Standalone PR body must not reference an issue"
    )
    assert sync._mappings_store.get("m-foo", {}).get("pull_request_number") == 7


def test_create_pr_with_mapping(sync, task_file):
    """Mapping with issue_id -> PR body contains 'Closes #<id>' (regression).

    Also asserts that the new persistence merges PR keys onto the existing
    mapping without resetting issue_id / issue_url.
    """
    sync._mappings_store["m-foo"] = {
        "issue_id": 42,
        "issue_url": "https://github.com/owner/repo/issues/42",
    }
    sync.get_task_mapping = lambda task_name: dict(sync._mappings_store.get(task_name, {}))

    captured, fake_pr, fake_create = _captured_create_pull_request(sync, task_file)

    with patch(
        "github.pr_manager.GitHubPRManager.create_pull_request",
        new=fake_create,
    ):
        with patch("github.task_sync.find_task_file", return_value=task_file):
            result = sync.create_pull_request_from_task(
                "m-foo", "fix/foo", target_branch="main"
            )

    assert result == fake_pr
    assert captured["title"] == "Resolve: Foo Title"
    assert "Closes #42" in captured["body"], (
        "Issue-linked PR body must reference the linked issue"
    )
    persisted = sync._mappings_store["m-foo"]
    assert persisted["issue_id"] == 42, "Existing issue_id must not be clobbered"
    assert persisted["issue_url"] == "https://github.com/owner/repo/issues/42"
    assert persisted["pull_request_number"] == 7, "PR number must be merged in"


def test_create_pr_with_mapping_missing_issue_id(sync, task_file):
    """Mapping present but no issue_id -> standalone path, no 'Closes #'."""
    sync.get_task_mapping = lambda task_name: {"some_other_key": "value"}

    captured, fake_pr, fake_create = _captured_create_pull_request(sync, task_file)

    with patch(
        "github.pr_manager.GitHubPRManager.create_pull_request",
        new=fake_create,
    ):
        with patch("github.task_sync.find_task_file", return_value=task_file):
            result = sync.create_pull_request_from_task(
                "m-foo", "fix/foo", target_branch="main"
            )

    assert result == fake_pr
    assert "Closes #" not in captured["body"]


def test_post_pr_consumers_no_op_when_no_issue_id(sync):
    """After standalone PR persistence, consumers must not KeyError on missing issue_id.

    Regression for codex finding: the new mapping persistence makes
    get_task_mapping() truthy without issue_id; consumers that did
    `mapping['issue_id']` unconditionally would raise.
    """
    sync._mappings_store["m-foo"] = {
        "pull_request_number": 7,
        "pull_request_url": "https://github.com/owner/repo/pull/7",
    }
    sync.get_task_mapping = lambda task_name: dict(sync._mappings_store.get(task_name, {}))

    # _add_code_review_comment_to_pr should silently no-op (issue_id is None)
    sync.github.add_comment = MagicMock()
    sync._extract_code_review_results = lambda task_name: "review text"
    result = sync._add_code_review_comment_to_pr(7, "m-foo")
    assert result is False
    sync.github.add_comment.assert_not_called()

    # sync_task_status_to_issue should return early without raising
    sync.github.update_issue = MagicMock()
    sync.sync_task_status_to_issue("m-foo", "completed")
    sync.github.update_issue.assert_not_called()

    # update_issue_description_from_task should raise the documented error,
    # not KeyError
    with pytest.raises(Exception, match="not linked to GitHub/Gitea issue"):
        sync.update_issue_description_from_task("m-foo")
