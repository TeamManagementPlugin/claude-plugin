#!/usr/bin/env python3
"""Phase-4 localized bug fixes (m-provider-layer-dedup).

Covers:
  - protocol_utils.get_project_root honors CLAUDE_PROJECT_DIR first.
  - GitLab default_branch is a lazy @property (no eager network call) with a
    setter (so tests/callers can pin it without a round-trip).
  - Jira sync_task_status_to_issue falls back to the numeric jira_issue_id when
    jira_issue_key is absent (a numeric-linked task must still sync).
  - GitHub sync_task_status_to_issue LOGS on failure instead of `except: pass`.
  - import_issue_as_task accepts update_mode on all three providers (no TypeError
    from the MCP issue_read tool, which passes update_mode to every provider).

Run with: python3 -m pytest test/test_provider_bugfixes.py -v
"""

import importlib
import inspect
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "plugin" / "hooks"
sys.path.insert(0, str(HOOKS_DIR))


# --------------------------------------------------------------- protocol_utils

def test_get_project_root_honors_env_first(tmp_path, monkeypatch):
    import protocol_utils
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    # cwd is elsewhere and has no .claude, but the env var wins.
    assert protocol_utils.get_project_root() == tmp_path


def test_get_project_root_ignores_missing_env(tmp_path, monkeypatch):
    import protocol_utils
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "does-not-exist"))
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    monkeypatch.chdir(project)
    # Bad env var is ignored; falls back to the cwd .claude-dir walk.
    assert protocol_utils.get_project_root() == project


# --------------------------------------------------------- GitLab default_branch

def test_gitlab_default_branch_lazy_and_settable():
    from gitlab.api import GitLabAPI
    api = GitLabAPI.__new__(GitLabAPI)
    api._cached_default_branch = None
    # The getter detects lazily; give it a stubbed _make_request so no network.
    api._make_request = MagicMock(return_value={"default_branch": "trunk"})
    api.project_path = "owner%2Frepo"
    assert api.default_branch == "trunk"

    # The setter pins the cache — no further detection / network.
    api._make_request.reset_mock()
    api.default_branch = "main"
    assert api._cached_default_branch == "main"
    assert api.default_branch == "main"
    api._make_request.assert_not_called()


# ------------------------------------------------------------------- Jira sync

def _jira_sync(tmp_path, mapping):
    jira_utils = importlib.import_module("jira_utils")
    sync = jira_utils.JiraTaskSync.__new__(jira_utils.JiraTaskSync)
    f = tmp_path / "jira-mappings.json"
    f.write_text(json.dumps({"m-x": mapping}), encoding="utf-8")
    sync.mappings_file = f
    sync.jira = MagicMock()
    return sync


def test_jira_sync_falls_back_to_numeric_id(tmp_path):
    # Linked by numeric id → jira_issue_key is absent/None; sync must still run.
    sync = _jira_sync(tmp_path, {"jira_issue_id": "12345"})
    sync.sync_task_status_to_issue("m-x", "in-progress")
    assert sync.jira.add_comment.called
    assert sync.jira.add_comment.call_args[0][0] == "12345"
    sync.jira.update_issue.assert_called_once()
    assert sync.jira.update_issue.call_args[0][0] == "12345"


def test_jira_sync_skips_when_no_id_or_key(tmp_path):
    # MR/PR-only mapping (no id, no key) still short-circuits.
    sync = _jira_sync(tmp_path, {"merge_request_number": 3})
    sync.sync_task_status_to_issue("m-x", "in-progress")
    sync.jira.add_comment.assert_not_called()


# ------------------------------------------------------------------ GitHub sync

def test_github_sync_logs_on_failure(tmp_path, monkeypatch):
    gts = importlib.import_module("github.task_sync")
    mock_log = MagicMock()
    monkeypatch.setattr(gts, "_log", mock_log)

    sync = gts.GitHubTaskSync.__new__(gts.GitHubTaskSync)
    f = tmp_path / "github-mappings.json"
    f.write_text(json.dumps({"m-x": {"issue_id": 5}}), encoding="utf-8")
    sync.mappings_file = f
    sync.github = MagicMock()
    sync.github.update_issue.side_effect = RuntimeError("boom")

    # Must NOT raise (graceful), but MUST log (no more bare `except: pass`).
    sync.sync_task_status_to_issue("m-x", "in-progress")
    assert mock_log.called


# ------------------------------------------------------------------ update_mode

def test_import_issue_accepts_update_mode_all_providers():
    from gitlab.task_sync import GitLabTaskSync
    from github.task_sync import GitHubTaskSync
    jira_utils = importlib.import_module("jira_utils")

    for cls in (GitLabTaskSync, GitHubTaskSync, jira_utils.JiraTaskSync):
        params = inspect.signature(cls.import_issue_as_task).parameters
        assert "update_mode" in params, f"{cls.__name__} missing update_mode param"


# --------------------------------------------------------------- _slugify Unicode

def test_slugify_preserves_unicode_and_falls_back():
    from issue_provider_base import IssueTrackingTaskSync as S
    # Unicode letters survive — an ASCII-only regex regressed these to an empty
    # slug, but GitLab/Jira's str.isalnum() had preserved them.
    assert S._slugify("Исправить баг") == "исправить-баг"
    assert S._slugify("Fix the bug!") == "fix-the-bug"
    assert S._slugify("a  b__c") == "a-b-c"       # runs (incl. underscore) collapse
    assert S._slugify("!!!") == "task"            # no alphanumerics → fallback, no `m-` collision
