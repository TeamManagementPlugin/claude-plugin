#!/usr/bin/env python3
"""
GitHub/Gitea core API wrapper.

Provides basic GitHub/Gitea API operations: issues, comments, label management.
Supports both GitHub and self-hosted Gitea instances with automatic detection.
"""
import re
from typing import Dict, List, Optional, Union

from issue_provider_base import IssueTrackingProvider
from shared_state import resolve_provider_token


class GitHubAPI(IssueTrackingProvider):
    """GitHub/Gitea API provider for team-management."""

    @property
    def provider_name(self) -> str:
        return "github"

    def __init__(self):
        """Initialize GitHub API with config from config.json"""
        super().__init__()

        if not self.config or not self.config.get('github', {}).get('enabled'):
            raise Exception("GitHub integration not enabled in config.json")

        github_config = self.config['github']
        self.api_token = resolve_provider_token('github', github_config.get('api_token'))
        self.base_url = github_config.get('base_url', 'https://api.github.com')
        self.repository = github_config.get('repository')  # owner/repo format
        self.username = github_config.get('username', 'x-access-token')
        self.default_branch_override = github_config.get('default_branch')
        self._cached_default_branch: Optional[str] = None
        self.default_labels = github_config.get('default_labels', ['claude-code', 'automated'])
        self.workflow_labels = github_config.get('workflow_labels', {
            'in_progress': 'in-progress',
            'blocked': 'blocked',
            'pending': 'pending'
        })
        self._label_cache: Optional[Dict[str, int]] = None

        if not self.api_token:
            raise Exception(
                "GitHub API token not found. Set it in "
                ".claude/state/provider-tokens.json (key: github) — a per-project "
                "file the AI cannot read — or github.api_token in config.json.")
        if not self.repository:
            raise Exception("GitHub repository not found in config.json")

        if not re.match(r'^[^/]+/[^/]+$', self.repository):
            raise Exception(f"Invalid repository format: {self.repository}. Expected: owner/repo")

        self.base_url = self.base_url.rstrip('/')
        self._warn_if_insecure_base_url(self.base_url)

        # Shared base _make_request builds URLs from self.api_base; GitHub's
        # endpoints already carry the full path, so api_base == base_url.
        self.api_base = self.base_url
        # Per-request timeout consumed by the shared base _make_request. GitHub
        # kept a shorter default; BCC_HTTP_TIMEOUT_SECONDS still overrides it.
        self.request_timeout = 15

        self.headers = {
            'Authorization': f'token {self.api_token}',
            'Accept': 'application/vnd.github.v3+json',
            'Content-Type': 'application/json'
        }

    @property
    def is_gitea(self) -> bool:
        """Detect if this is a Gitea instance based on base_url."""
        base_url_lower = self.base_url.lower()
        return '/api/v1' in base_url_lower or 'gitea' in base_url_lower

    def _get_label_name_to_id_map(self) -> Dict[str, int]:
        """Fetch repository labels and build name-to-ID mapping (for Gitea).

        Paginates through every page of labels — a repo with more than one page
        of labels (>30 by default) would otherwise yield an incomplete map and
        cause `_convert_labels_for_gitea` to recreate already-existing labels.
        """
        if self._label_cache is not None:
            return self._label_cache

        per_page = 100
        page = 1
        label_map: Dict[str, int] = {}
        complete = True
        try:
            while True:
                result = self._make_request(
                    'GET', f"/repos/{self.repository}/labels?per_page={per_page}&page={page}"
                )
                if not isinstance(result, list):
                    # Error or unexpected shape — do not cache so the next call retries.
                    if page == 1:
                        return {}
                    # A later page failed: the map is INCOMPLETE. Do NOT cache it —
                    # a partial cache would make _convert_labels_for_gitea recreate
                    # already-existing labels (the bug this guards; the previous
                    # `break` fell through to the unconditional cache below).
                    complete = False
                    break
                for label in result:
                    if 'name' in label and 'id' in label:
                        label_map[label['name']] = label['id']
                if len(result) < per_page:
                    break
                page += 1
            if complete:
                self._label_cache = label_map
            return label_map
        except Exception:
            pass
        return {}

    def _create_label(self, name: str, color: str = "#0052CC", description: str = "") -> Optional[int]:
        """Create a label in Gitea repository."""
        try:
            label_data = {
                'name': name,
                'color': color.lstrip('#'),
                'description': description
            }
            result = self._make_request('POST', f"/repos/{self.repository}/labels", label_data)
            return result.get('id') if result and not result.get('error') else None
        except Exception as e:
            # Log error but don't fail entire operation
            print(f"Warning: Failed to create label '{name}': {e}")
            return None

    def _convert_labels_for_gitea(self, label_names: List[str], auto_create: bool = True) -> List[int]:
        """Convert label names to IDs for Gitea, optionally creating missing labels."""
        if not label_names:
            return []

        label_map = self._get_label_name_to_id_map()
        missing_labels = [name for name in label_names if name not in label_map]

        if missing_labels and auto_create:
            for name in missing_labels:
                label_id = self._create_label(name, description="Auto-created by Claude Code")
                if label_id:
                    label_map[name] = label_id
                    self._label_cache = label_map

        return [label_map[name] for name in label_names if name in label_map]

    def get_default_branch(self) -> str:
        """Return repository default branch, fetching from API if needed."""
        if self._cached_default_branch:
            return self._cached_default_branch

        if self.default_branch_override:
            self._cached_default_branch = self.default_branch_override
            return self._cached_default_branch

        try:
            repo_info = self._make_request('GET', f"/repos/{self.repository}")
            if repo_info and not repo_info.get('error'):
                default_branch = repo_info.get('default_branch', 'main')
                self._cached_default_branch = default_branch
                return default_branch
        except Exception:
            pass

        self._cached_default_branch = 'main'
        return 'main'

    def get_issue(self, issue_id: Union[str, int]) -> Optional[Dict]:
        """Get a specific GitHub issue by number."""
        issue_id = self.extract_issue_id(issue_id)

        if not str(issue_id).isdigit():
            raise ValueError(f"Invalid issue_id format: {issue_id}")

        result = self._make_request('GET', f"/repos/{self.repository}/issues/{issue_id}")

        return self._raise_on_error(result, f"Failed to get issue #{issue_id}")

    def create_issue(self, title: str, description: str = "", labels: List[str] = None,
                    issue_type: str = None) -> Optional[Dict]:
        """Create a new GitHub/Gitea issue."""
        if not title or not isinstance(title, str):
            raise ValueError("Issue title must be a non-empty string")
        if len(title) > 256:
            raise ValueError("Issue title too long (max 256 characters)")

        issue_data = {
            'title': title,
            'body': description or ""
        }

        label_names = labels if labels else self.default_labels

        if label_names:
            if self.is_gitea:
                label_ids = self._convert_labels_for_gitea(label_names)
                if label_ids:
                    issue_data['Labels'] = label_ids
            else:
                issue_data['labels'] = label_names

        result = self._make_request('POST', f"/repos/{self.repository}/issues", issue_data)

        return self._raise_on_error(result, "Failed to create issue")

    def _set_issue_labels_gitea(self, issue_id: Union[str, int], label_ids: List[int]) -> Optional[Dict]:
        """Set labels on a Gitea issue using the dedicated labels endpoint."""
        result = self._make_request(
            'PUT',
            f"/repos/{self.repository}/issues/{issue_id}/labels",
            {'labels': label_ids}
        )

        if isinstance(result, list):
            return {
                'success': True,
                'labels_updated': True,
                'labels': [label.get('name') for label in result if isinstance(label, dict)]
            }

        return self._raise_on_error(result, f"Failed to set labels on issue #{issue_id}")

    def update_issue(self, issue_id: Union[str, int], title: str = None,
                    description: str = None, status: str = None,
                    labels: List[str] = None) -> Optional[Dict]:
        """Update an existing GitHub/Gitea issue."""
        issue_id = self.extract_issue_id(issue_id)

        if not str(issue_id).isdigit():
            raise ValueError(f"Invalid issue_id format: {issue_id}")

        update_data = {}

        if title is not None:
            update_data['title'] = title
        if description is not None:
            update_data['body'] = description
        if status:
            if status.lower() in ['open', 'closed']:
                update_data['state'] = status.lower()

        labels_updated = False
        if labels is not None:
            if self.is_gitea:
                label_ids = self._convert_labels_for_gitea(labels)
                if label_ids:
                    self._set_issue_labels_gitea(issue_id, label_ids)
                    labels_updated = True
            else:
                update_data['labels'] = labels
                labels_updated = True

        if not update_data and labels_updated:
            return {'success': True, 'labels_updated': True}

        if not update_data:
            return None

        result = self._make_request('PATCH', f"/repos/{self.repository}/issues/{issue_id}", update_data)

        return self._raise_on_error(result, f"Failed to update issue #{issue_id}")

    def add_comment(self, issue_id: Union[str, int], comment: str) -> Optional[Dict]:
        """Add a comment to a GitHub issue."""
        issue_id = self.extract_issue_id(issue_id)

        if not str(issue_id).isdigit():
            raise ValueError(f"Invalid issue_id format: {issue_id}")
        if not comment or not isinstance(comment, str):
            raise ValueError("Comment must be a non-empty string")

        comment_data = {'body': comment}
        result = self._make_request('POST', f"/repos/{self.repository}/issues/{issue_id}/comments", comment_data)

        return self._raise_on_error(result, f"Failed to add comment to issue #{issue_id}")

    def parse_issue_url(self, url: str) -> Optional[Dict[str, str]]:
        """Parse GitHub issue URL to extract components."""
        patterns = [
            r'github\.com/([^/]+)/([^/]+)/issues/(\d+)',
            r'([^/]+)/([^/]+)/([^/]+)/issues/(\d+)',
            r'api\.github\.com/repos/([^/]+)/([^/]+)/issues/(\d+)',
        ]

        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                issue_number = match.groups()[-1]
                return {'issue_id': issue_number, 'url': url}

        return None

    def format_issue_id(self, issue_id: Union[str, int]) -> str:
        """Format issue ID with provider prefix."""
        return f"github:{issue_id}"

    def extract_issue_id(self, formatted_id: Union[str, int]) -> Union[str, int]:
        """Extract the actual issue ID from a provider-prefixed ID."""
        formatted_id_str = str(formatted_id)
        if formatted_id_str.startswith('github:'):
            return formatted_id_str[7:]
        return formatted_id

    def get_supported_issue_types(self) -> List[str]:
        """Get list of supported issue types."""
        return ["issue"]

    def validate_configuration(self) -> bool:
        """Validate that GitHub is properly configured and accessible."""
        try:
            result = self._make_request('GET', '/user')
            if result and result.get('error'):
                return False

            repo_result = self._make_request('GET', f"/repos/{self.repository}")
            if repo_result and repo_result.get('error'):
                return False

            return True
        except Exception:
            return False
