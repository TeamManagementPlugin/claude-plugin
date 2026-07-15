#!/usr/bin/env python3
"""
GitLab core API wrapper.

Provides basic GitLab API operations: issues, comments, and HTTP request handling.
"""
import urllib.parse
import subprocess
import re
from typing import Dict, List, Optional, Union

from issue_provider_base import IssueTrackingProvider


from shared_state import get_provider_logger, resolve_provider_token

_log = get_provider_logger('gitlab.log')


class GitLabAPI(IssueTrackingProvider):
    """GitLab API wrapper for team-management.

    Inherits from IssueTrackingProvider for standardized interface.
    """

    @property
    def provider_name(self) -> str:
        return "gitlab"

    def __init__(self):
        """Initialize GitLab API with config from config.json"""
        super().__init__()  # Calls base class _find_project_root() and _load_config()

        if not self.config or not self.config.get('gitlab', {}).get('enabled'):
            raise Exception("GitLab integration not enabled in config.json")

        gitlab_config = self.config['gitlab']
        self.api_token = resolve_provider_token('gitlab', gitlab_config.get('api_token'))
        self.base_url = gitlab_config.get('base_url', 'https://gitlab.com')

        raw_project_path = gitlab_config.get('project_path')
        self.raw_project_path = raw_project_path  # Keep unencoded version
        self.project_path = urllib.parse.quote(raw_project_path, safe='') if raw_project_path else None
        self.default_labels = gitlab_config.get('default_labels', ['claude-code', 'automated'])

        if not self.api_token:
            raise Exception(
                "GitLab API token not found. Set it in "
                ".claude/state/provider-tokens.json (key: gitlab) — a per-project "
                "file the AI cannot read — or gitlab.api_token in config.json.")
        if not self.project_path:
            raise Exception("GitLab project path not found in config.json")

        # Remove trailing slash from base_url
        self.base_url = self.base_url.rstrip('/')
        self._warn_if_insecure_base_url(self.base_url)
        self.api_base = f"{self.base_url}/api/v4"

        # Setup headers for API calls
        self.headers = {
            'PRIVATE-TOKEN': self.api_token,
            'Content-Type': 'application/json'
        }

        # Initialize cache for default branch detection
        self._cached_default_branch = None

        # Per-request timeout consumed by the shared base _make_request
        self.request_timeout = 30

        # NOTE: default_branch is a lazy @property (defined below) — it is NOT
        # detected here, so constructing GitLabAPI() makes no network call.

    def _detect_default_branch(self) -> str:
        """Auto-detect default branch with multiple strategies."""
        # Check cache first
        if self._cached_default_branch:
            return self._cached_default_branch

        # Strategy 1: Query GitLab API for project default branch
        try:
            project_info = self._make_request('GET', f"/projects/{self.project_path}")
            if project_info and not project_info.get('error'):
                default_branch = project_info.get('default_branch', 'master')
                self._cached_default_branch = default_branch
                return default_branch
        except Exception as e:
            _log(f"Warning: Could not detect default branch via GitLab API: {e}")

        # Strategy 2: Check remote HEAD symbolic reference
        try:
            result = subprocess.run(
                ['git', 'symbolic-ref', 'refs/remotes/origin/HEAD'],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                cwd=self.project_root,
                timeout=5
            )
            if result.returncode == 0:
                ref = result.stdout.strip()
                branch = ref.split('/')[-1]
                if branch:
                    self._cached_default_branch = branch
                    return branch
        except Exception:
            pass

        # Strategy 3: Check existing branches
        try:
            result = subprocess.run(
                ['git', 'branch', '-a'],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                cwd=self.project_root,
                timeout=5
            )
            if result.returncode == 0:
                branches = result.stdout.lower()

                if re.search(r'\bremotes/origin/main\b', branches):
                    self._cached_default_branch = 'main'
                    return 'main'
                elif re.search(r'\bremotes/origin/master\b', branches):
                    self._cached_default_branch = 'master'
                    return 'master'
        except Exception:
            pass

        # Fallback to 'master'
        self._cached_default_branch = 'master'
        return 'master'

    @property
    def default_branch(self) -> str:
        """Default branch, detected LAZILY on first access (result cached).

        Previously eager in __init__ — a blocking network call on every
        GitLabAPI() construction (issue_status, auto-sync, …). Deferred so
        constructing the provider is cheap; the setter lets callers/tests pin a
        value without a network round-trip.
        """
        return self._detect_default_branch()

    @default_branch.setter
    def default_branch(self, value: str) -> None:
        self._cached_default_branch = value

    def get_issue(self, issue_id: Union[str, int]) -> Optional[Dict]:
        """Get a specific GitLab issue."""
        # Input validation
        if not str(issue_id).isdigit() and not str(issue_id).isalnum():
            raise ValueError(f"Invalid issue_id format: {issue_id}")

        result = self._make_request('GET', f"/projects/{self.project_path}/issues/{issue_id}")

        return self._raise_on_error(result, "GitLab API error")

    def create_issue(self, title: str, description: str = "", labels: List[str] = None) -> Optional[Dict]:
        """Create a new GitLab issue."""
        # Input validation
        if not title or len(title.strip()) == 0:
            raise ValueError("Issue title cannot be empty")
        if len(title) > 255:
            raise ValueError("Issue title too long (max 255 characters)")

        if labels is None:
            labels = self.default_labels

        data = {
            'title': title.strip(),
            'description': description,
            'labels': ','.join(labels) if labels else ''
        }

        result = self._make_request('POST', f"/projects/{self.project_path}/issues", data)

        return self._raise_on_error(result, "GitLab API error creating issue")

    def update_issue(self, issue_id: Union[str, int], title: str = None, description: str = None,
                    state_event: str = None, labels: List[str] = None) -> Optional[Dict]:
        """Update an existing GitLab issue."""
        data = {}

        if title is not None:
            data['title'] = title
        if description is not None:
            data['description'] = description
        if state_event is not None:
            data['state_event'] = state_event
        if labels is not None:
            data['labels'] = ','.join(labels)

        result = self._make_request('PUT', f"/projects/{self.project_path}/issues/{issue_id}", data)

        return self._raise_on_error(result, "GitLab API error updating issue")

    def add_comment(self, issue_id: Union[str, int], comment: str) -> Optional[Dict]:
        """Add a comment to a GitLab issue (base class method)."""
        return self.add_issue_comment(issue_id, comment)

    def add_issue_comment(self, issue_iid: Union[str, int], comment: str) -> Optional[Dict]:
        """Add a comment to a GitLab issue using project-scoped IID."""
        data = {'body': comment}
        result = self._make_request('POST', f"/projects/{self.project_path}/issues/{issue_iid}/notes", data)

        return self._raise_on_error(result, "GitLab API error adding comment")

    def parse_issue_url(self, url: str) -> Optional[Dict[str, str]]:
        """Parse GitLab issue URL to extract components."""
        # Delegate to module-level function
        from . import parse_gitlab_issue_url
        return parse_gitlab_issue_url(url)

    def format_issue_id(self, issue_id: Union[str, int]) -> str:
        """Format issue ID with provider prefix."""
        return f"gitlab:{issue_id}"

    def extract_issue_id(self, formatted_id: str) -> Union[str, int]:
        """Extract issue ID from provider-prefixed format."""
        if formatted_id.startswith("gitlab:"):
            return int(formatted_id[7:])
        return int(formatted_id)

    def get_supported_issue_types(self) -> List[str]:
        """Get list of supported issue types for GitLab."""
        return ["issue", "incident", "task"]

    def validate_configuration(self) -> bool:
        """Validate that GitLab is properly configured."""
        try:
            # Test API connectivity by fetching project info
            project_info = self._make_request('GET', f"/projects/{self.project_path}")
            return project_info is not None and not project_info.get('error')
        except Exception:
            return False
