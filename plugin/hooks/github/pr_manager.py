#!/usr/bin/env python3
"""
GitHub/Gitea pull request management.

Handles PR creation, updates, descriptions, and branch-based queries.
"""
import urllib.parse
from typing import Dict, Optional, Union, TYPE_CHECKING

if TYPE_CHECKING:
    from .api import GitHubAPI

from issue_provider_base import find_task_file
from git_operations import get_commit_log


class GitHubPRManager:
    """Manages GitHub/Gitea pull request operations."""

    def __init__(self, api: 'GitHubAPI'):
        """Initialize with GitHub API instance.

        Args:
            api: GitHubAPI instance for making requests
        """
        self.api = api
        self.project_root = api.project_root
        self.repository = api.repository

    def _generate_commit_aggregation(self, source_branch: str, target_branch: str = None) -> str:
        """Generate aggregated summary of commits on a branch."""
        if target_branch is None:
            target_branch = self.api.get_default_branch()

        commits = get_commit_log(source_branch, target_branch, self.project_root)
        if commits:
            formatted = [f"- {commit}" for commit in commits]
            return "## Implementation Changes\n\n" + '\n'.join(formatted)

        return ""

    def _generate_task_summary(self, task_name: str) -> str:
        """Generate implementation summary from task success criteria."""
        if not task_name:
            return ""

        task_file = find_task_file(self.project_root, task_name)
        if not task_file:
            return ""

        try:
            with open(task_file, 'r', encoding='utf-8') as f:
                content = f.read()

            lines = content.split('\n')
            success_criteria = []
            in_success_section = False

            for line in lines:
                line_stripped = line.strip()

                if line_stripped.startswith('## Success Criteria'):
                    in_success_section = True
                    continue
                elif line_stripped.startswith('##') and in_success_section:
                    break
                elif in_success_section and line_stripped.startswith('- [x]'):
                    criteria = line_stripped[6:].strip()
                    success_criteria.append(f"- {criteria}")

            if success_criteria:
                return "## Implementation Summary\n\n" + '\n'.join(success_criteria)

        except Exception:
            pass

        return ""

    def generate_enhanced_pr_description(self, issue_id: Union[str, int], source_branch: str,
                                        target_branch: str = None, task_name: str = None,
                                        commit_body: str = None) -> str:
        """Generate enhanced PR description with commit aggregation and task summary."""
        if target_branch is None:
            target_branch = self.api.get_default_branch()

        description_parts = []

        # Add issue reference
        if issue_id:
            description_parts.append(f"Closes #{issue_id}")

        # Add commit body if provided
        if commit_body:
            description_parts.append(f"## Description\n\n{commit_body}")

        # Add task summary
        if task_name:
            task_summary = self._generate_task_summary(task_name)
            if task_summary:
                description_parts.append(task_summary)

        # Add commit aggregation
        commit_summary = self._generate_commit_aggregation(source_branch, target_branch)
        if commit_summary:
            description_parts.append(commit_summary)

        description_parts.append("---\nGenerated with [Claude Code](https://claude.ai/code)")

        return '\n\n'.join(description_parts)

    def create_pull_request(self, title: str, body: str, head: str, base: str = None) -> Optional[Dict]:
        """Create a GitHub/Gitea Pull Request."""
        if not title or not isinstance(title, str):
            raise ValueError("PR title must be a non-empty string")
        if len(title) > 256:
            raise ValueError("PR title too long (max 256 characters)")

        if base is None:
            base = self.api.get_default_branch()

        pr_data = {
            'title': title,
            'body': body or "",
            'head': head,
            'base': base
        }

        result = self.api._make_request('POST', f"/repos/{self.repository}/pulls", pr_data)

        return self.api._raise_on_error(result, "Failed to create pull request")

    def find_pull_request_by_branch(self, head_branch: str, state: str = "open") -> Optional[Dict]:
        """Find pull request by head branch."""
        if not head_branch or not isinstance(head_branch, str):
            raise ValueError("Head branch must be a non-empty string")

        valid_states = {"open", "closed", "all"}
        if state and state not in valid_states:
            raise ValueError(f"Invalid state: {state}")

        owner = self.repository.split('/')[0]
        head_param = f"{owner}:{head_branch}"
        endpoint = f"/repos/{self.repository}/pulls?head={urllib.parse.quote(head_param)}&per_page=100"

        if state:
            endpoint += f"&state={state}"

        result = self.api._make_request('GET', endpoint)

        # A successful list endpoint returns a list; error responses are dicts.
        self.api._raise_on_error(result, "Failed to find pull request")

        if result and isinstance(result, list) and len(result) > 0:
            return result[0]

        return None

    def add_pr_comment(self, pr_number: int, comment: str) -> Optional[Dict]:
        """Add a comment to a GitHub pull request."""
        comment_data = {'body': comment}
        result = self.api._make_request('POST', f"/repos/{self.repository}/issues/{pr_number}/comments", comment_data)

        return self.api._raise_on_error(result, "Failed to add PR comment")
