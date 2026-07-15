#!/usr/bin/env python3
"""
GitHub/Gitea API utilities for team-management integration.

BACKWARD COMPATIBILITY WRAPPER: This file now imports from the modular
github package (github/api.py, github/task_sync.py, etc.) to maintain
backward compatibility with existing code that imports from github_utils.py.

For new code, prefer importing directly from the github package:
    from github import GitHubAPI, GitHubTaskSync

This wrapper will be maintained for compatibility with hooks and MCP server.
"""
import sys
from pathlib import Path
from typing import Optional

# Setup path for imports
current = Path(__file__).resolve()
project_root = current.parents[1]  # Go up to plugin/
sys.path.insert(0, str(project_root / 'hooks'))

# Import from modular package
from github import (
    GitHubAPI,
    GitHubTaskSync
)

# Re-export the manager classes so callers (hooks / MCP server) can construct a
# manager wrapping an API instance without reaching into the github subpackage.
# The PR/release methods live on these managers after the provider split; bare
# GitHubAPI() no longer carries them. (No circular import: the manager modules
# import GitHubAPI only under TYPE_CHECKING.)
from github.pr_manager import GitHubPRManager
from github.releases import GitHubReleaseManager

# Re-export for backward compatibility
__all__ = [
    'GitHubAPI',
    'GitHubTaskSync',
    'GitHubPRManager',
    'GitHubReleaseManager',
    'get_github_api',
    'get_github_sync',
    '_clear_singletons'
]


# Singleton instances
_github_api_instance = None
_github_sync_instance = None


def get_github_api() -> Optional[GitHubAPI]:
    """Get singleton GitHub API instance.

    Backward compatibility function. Returns None if GitHub not enabled.
    """
    global _github_api_instance
    if _github_api_instance is None:
        try:
            _github_api_instance = GitHubAPI()
        except Exception:
            return None
    return _github_api_instance


def get_github_sync() -> Optional[GitHubTaskSync]:
    """Get singleton GitHub task sync instance.

    Backward compatibility function. Returns None if GitHub not enabled.
    """
    global _github_sync_instance
    if _github_sync_instance is None:
        try:
            _github_sync_instance = GitHubTaskSync()
        except Exception:
            return None
    return _github_sync_instance


def _clear_singletons() -> None:
    """Clear singleton instances to force re-initialization.

    Called by config.reload_config() to ensure provider instances
    pick up new configuration after config file changes.
    """
    global _github_api_instance, _github_sync_instance
    _github_api_instance = None
    _github_sync_instance = None
