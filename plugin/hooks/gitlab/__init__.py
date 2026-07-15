#!/usr/bin/env python3
"""
GitLab integration package for team-management.

Provides modular GitLab API integration with separate concerns:
- api.py: Core GitLab API (issues, comments)
- mr_manager.py: Merge request operations
- releases.py: Release and asset management
- task_sync.py: Task-to-issue synchronization

Public API exports maintain backward compatibility with gitlab_utils.py.
"""
import re
from typing import Dict, Optional

# Core API
from .api import GitLabAPI

# Task synchronization
from .task_sync import GitLabTaskSync

def parse_gitlab_issue_url(url: str) -> Optional[Dict[str, str]]:
    """Parse GitLab issue URL to extract components.

    Args:
        url: GitLab issue URL like https://gitlab.com/namespace/project/-/issues/123

    Returns:
        Dict with base_url, project_path, issue_id keys, or None if invalid
    """
    # Pattern to match GitLab issue URLs
    pattern = r'^(https?://[^/]+)/((?:[^/]+/)+[^/]+)/-/issues/(\d+)(?:[/#?].*)?$'

    match = re.match(pattern, url.strip())
    if not match:
        return None

    base_url, project_path, issue_id = match.groups()

    return {
        'base_url': base_url,
        'project_path': project_path,
        'issue_id': issue_id
    }


# Public exports
__all__ = [
    'GitLabAPI',
    'GitLabTaskSync',
    'parse_gitlab_issue_url'
]
