#!/usr/bin/env python3
"""
GitLab API utilities for team-management integration.

BACKWARD COMPATIBILITY WRAPPER: This file now imports from the modular
gitlab package (gitlab/api.py, gitlab/task_sync.py, etc.) to maintain
backward compatibility with existing code that imports from gitlab_utils.py.

For new code, prefer importing directly from the gitlab package:
    from gitlab import GitLabAPI, GitLabTaskSync

This wrapper will be maintained for compatibility with hooks and MCP server.
"""
import sys
from pathlib import Path

# Setup path for imports
current = Path(__file__).resolve()
project_root = current.parents[1]  # Go up to plugin/
sys.path.insert(0, str(project_root / 'hooks'))

# Import from modular package
from gitlab import (
    GitLabAPI,
    GitLabTaskSync,
    parse_gitlab_issue_url
)

# Re-export the manager classes so callers (hooks / MCP server) can construct a
# manager wrapping an API instance without reaching into the gitlab subpackage.
# The MR/release methods live on these managers after the provider split; bare
# GitLabAPI() no longer carries them. (No circular import: the manager modules
# import GitLabAPI only under TYPE_CHECKING.)
from gitlab.mr_manager import GitLabMRManager
from gitlab.releases import GitLabReleaseManager
from shared_state import get_provider_logger
from typing import Optional

_log = get_provider_logger('gitlab.log')

# Re-export for backward compatibility
__all__ = [
    'GitLabAPI',
    'GitLabTaskSync',
    'GitLabMRManager',
    'GitLabReleaseManager',
    'parse_gitlab_issue_url',
    'get_gitlab_sync'
]


def get_gitlab_sync() -> Optional[GitLabTaskSync]:
    """Get a GitLabTaskSync instance, or None if GitLab is not enabled/configured.

    Unified factory contract (m-provider-layer-dedup): returns None + logs on
    failure, matching get_github_sync() / get_jira_sync(). Previously this RAISED,
    forcing every caller to special-case GitLab with a try/except; callers now
    check for None uniformly across all three providers.
    """
    try:
        return GitLabTaskSync()
    except Exception as e:
        _log(f"GitLab sync initialization failed: {e}")
        return None
