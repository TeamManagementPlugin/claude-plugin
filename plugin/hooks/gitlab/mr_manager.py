#!/usr/bin/env python3
"""
GitLab merge request management.

Handles MR creation, updates, descriptions, and branch-based queries.
"""
import urllib.parse
from typing import Dict, List, Optional, Union, TYPE_CHECKING
from git_operations import get_commit_log

if TYPE_CHECKING:
    from .api import GitLabAPI

from issue_provider_base import find_task_file


class GitLabMRManager:
    """Manages GitLab merge request operations."""

    def __init__(self, api: 'GitLabAPI'):
        """Initialize with GitLab API instance.

        Args:
            api: GitLabAPI instance for making requests
        """
        self.api = api
        self.project_root = api.project_root
        self.project_path = api.project_path
        self.default_branch = api.default_branch
        self.default_labels = api.default_labels

    def _generate_commit_aggregation(self, source_branch: str, target_branch: str = None) -> str:
        """Generate aggregated summary of commits on a branch."""
        # Use auto-detected default branch if not specified
        if target_branch is None:
            target_branch = self.default_branch

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
                    # Extract completed success criteria
                    criteria = line_stripped[6:].strip()  # Remove "- [x] "
                    success_criteria.append(f"- {criteria}")

            if success_criteria:
                return "## Implementation Summary\n\n" + '\n'.join(success_criteria)

        except Exception:
            pass

        return ""

    def generate_enhanced_mr_description(self, issue_iid: Union[str, int], source_branch: str,
                                        target_branch: str = None, task_name: str = None,
                                        commit_body: str = None) -> str:
        """Generate enhanced MR description with commit aggregation and task summary.

        Args:
            issue_iid: GitLab issue IID (can be None for non-issue MRs)
            source_branch: Source branch name
            target_branch: Target branch name (default: auto-detected)
            task_name: Task name for enhanced description
            commit_body: Optional detailed commit explanation

        Returns:
            Formatted MR description with markdown
        """
        # Use auto-detected default branch if not specified
        if target_branch is None:
            target_branch = self.default_branch

        # Start with proper GitLab auto-close syntax (if issue provided)
        description_parts = []
        if issue_iid:
            description_parts.append(f"Closes #{issue_iid}")

        # Add detailed commit explanation if provided
        if commit_body:
            description_parts.append(f"## Description\n\n{commit_body}")

        # Add task summary if available
        if task_name:
            task_summary = self._generate_task_summary(task_name)
            if task_summary:
                description_parts.append(task_summary)

        # Add commit aggregation
        commit_summary = self._generate_commit_aggregation(source_branch, target_branch)
        if commit_summary:
            description_parts.append(commit_summary)

        # Add metadata footer
        description_parts.append("---\nGenerated with [Claude Code](https://claude.ai/code)")

        return '\n\n'.join(description_parts)

    def create_merge_request(self, issue_iid: Union[str, int], source_branch: str,
                           target_branch: str = None, title: str = None, task_name: str = None,
                           labels: List[str] = None, commit_body: str = None) -> Optional[Dict]:
        """Create a merge request linked to a GitLab issue.

        Args:
            issue_iid: GitLab issue IID
            source_branch: Source branch name
            target_branch: Target branch name (default: auto-detected)
            title: MR title (default: auto-generated from issue)
            task_name: Task name for enhanced description
            labels: Labels to apply (default: inherit from issue + default_labels)
            commit_body: Optional detailed commit explanation for MR description

        Returns:
            MR data dict or None on error
        """
        # Use auto-detected default branch if not specified
        if target_branch is None:
            target_branch = self.default_branch

        # Get issue details for title and labels
        issue = self.api.get_issue(issue_iid) if title is None or labels is None else None

        if title is None:
            if issue:
                title = f"Resolve \"{issue['title']}\""
            else:
                title = f"Merge {source_branch}"

        # Determine labels: priority is explicit > issue labels + defaults > defaults only
        if labels is None:
            mr_labels = list(self.default_labels)  # Start with defaults
            if issue and issue.get('labels'):
                # Inherit labels from issue for consistency
                issue_labels = issue['labels'] if isinstance(issue['labels'], list) else [issue['labels']]
                # Add issue labels that aren't already in defaults
                for label in issue_labels:
                    label_name = label['name'] if isinstance(label, dict) else label
                    if label_name not in mr_labels:
                        mr_labels.append(label_name)
        else:
            mr_labels = labels

        # Generate enhanced description
        description = self.generate_enhanced_mr_description(issue_iid, source_branch, target_branch, task_name, commit_body)

        data = {
            'source_branch': source_branch,
            'target_branch': target_branch,
            'title': title,
            'description': description,
            'labels': ','.join(mr_labels) if mr_labels else '',
            'remove_source_branch': True,  # Clean up after merge
            'squash': True  # Squash commits for cleaner history
        }

        result = self.api._make_request('POST', f"/projects/{self.project_path}/merge_requests", data)

        # Check for error response and raise exception with details
        return self.api._raise_on_error(result, "GitLab API error creating merge request")

    def get_merge_request(self, mr_iid: Union[str, int]) -> Optional[Dict]:
        """Get a specific GitLab merge request."""
        result = self.api._make_request('GET', f"/projects/{self.project_path}/merge_requests/{mr_iid}")

        return self.api._raise_on_error(result, "GitLab API error getting merge request")

    def get_mr_notes(self, mr_iid: Union[str, int]) -> List[Dict]:
        """Get all notes/comments from a GitLab merge request.

        Args:
            mr_iid: Merge request IID (project-specific ID)

        Returns:
            List of note objects across all pages, or empty list if none found
        """
        per_page = 100
        page = 1
        all_notes: List[Dict] = []

        while True:
            endpoint = (
                f"/projects/{self.project_path}/merge_requests/{mr_iid}/notes"
                f"?per_page={per_page}&page={page}"
            )
            result = self.api._make_request('GET', endpoint)

            # Check for error response (error responses are dicts, not lists)
            self.api._raise_on_error(result, "GitLab API error fetching MR notes")

            if not (result and isinstance(result, list)):
                break

            all_notes.extend(result)
            if len(result) < per_page:
                break
            page += 1

        return all_notes

    def update_merge_request(self, mr_iid: Union[str, int], title: str = None,
                           description: str = None, state_event: str = None) -> Optional[Dict]:
        """Update an existing GitLab merge request."""
        data = {}

        if title is not None:
            data['title'] = title
        if description is not None:
            data['description'] = description
        if state_event is not None:  # 'close' or 'reopen'
            data['state_event'] = state_event

        result = self.api._make_request('PUT', f"/projects/{self.project_path}/merge_requests/{mr_iid}", data)

        return self.api._raise_on_error(result, "GitLab API error updating merge request")

    def add_mr_comment(self, mr_iid: Union[str, int], comment: str) -> Optional[Dict]:
        """Add a comment to a GitLab merge request."""
        data = {'body': comment}
        result = self.api._make_request('POST', f"/projects/{self.project_path}/merge_requests/{mr_iid}/notes", data)

        return self.api._raise_on_error(result, "GitLab API error adding MR comment")

    def find_merge_request_by_branch(self, source_branch: str, state: str = "opened") -> Optional[Dict]:
        """Find merge request by source branch.

        Args:
            source_branch: Source branch name to search for
            state: MR state filter (opened, closed, merged, all). Default: opened

        Returns:
            First matching MR dict or None if not found
        """
        # Input validation
        if not source_branch or not isinstance(source_branch, str):
            raise ValueError("Source branch must be a non-empty string")

        # Validate state parameter
        valid_states = {"opened", "closed", "merged", "all"}
        if state and state not in valid_states:
            raise ValueError(f"Invalid state: {state}. Must be one of: {', '.join(valid_states)}")

        # Query for MRs with this source branch
        endpoint = f"/projects/{self.project_path}/merge_requests?source_branch={urllib.parse.quote(source_branch)}&per_page=100"

        if state:
            endpoint += f"&state={state}"

        result = self.api._make_request('GET', endpoint)

        # A successful list endpoint returns a list; error responses are dicts.
        self.api._raise_on_error(result, "GitLab API error finding merge request")

        # Result is a list of MRs, return first one if exists
        if result and isinstance(result, list) and len(result) > 0:
            return result[0]

        return None
