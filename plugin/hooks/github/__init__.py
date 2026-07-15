#!/usr/bin/env python3
"""
GitHub/Gitea integration package for team-management.

Provides modular GitHub/Gitea API integration with separate concerns:
- api.py: Core GitHub/Gitea API (issues, comments, label management)
- pr_manager.py: Pull request operations
- releases.py: Release and asset management
- task_sync.py: Task-to-issue synchronization

Supports both GitHub and self-hosted Gitea instances with automatic detection.

Public API exports maintain backward compatibility with github_utils.py.
"""
# Core API
from .api import GitHubAPI

# Task synchronization
from .task_sync import GitHubTaskSync

# Public exports
__all__ = [
    'GitHubAPI',
    'GitHubTaskSync'
]
