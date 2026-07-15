#!/usr/bin/env python3
"""
GitHub/Gitea task synchronization.

Handles bidirectional sync between Claude tasks and GitHub/Gitea issues.
"""
import re
from datetime import datetime
from typing import Dict, Optional, Union

from issue_provider_base import IssueTrackingTaskSync, find_task_file
from shared_state import get_provider_logger

_log = get_provider_logger('github.log')


class GitHubTaskSync(IssueTrackingTaskSync):
    """Task synchronization for GitHub/Gitea issues."""

    def __init__(self):
        """Initialize with GitHub API provider"""
        from .api import GitHubAPI
        self.github = GitHubAPI()
        super().__init__(self.github)

    def link_task_to_issue(self, task_name: str, issue_id: Union[str, int],
                          sync_direction: str = "bidirectional") -> bool:
        """Create a mapping between a task and a GitHub issue."""
        try:
            issue = self.github.get_issue(issue_id)
            if not issue:
                raise Exception(f"GitHub issue #{issue_id} not found")

            def _mut(mappings):
                mappings[task_name] = {
                    'issue_id': str(issue_id),
                    'issue_url': issue['html_url'],
                    'linked_at': datetime.now().isoformat(),
                    'sync_direction': sync_direction
                }
                return True
            self._locked_mapping_update(_mut)
            return True
        except Exception:
            return False

    @staticmethod
    def _strip_yaml_frontmatter(text: str) -> str:
        """Strip YAML frontmatter from text."""
        if not text:
            return text
        pattern = r'^---\s*\n.*?\n---\s*\n'
        result = re.sub(pattern, '', text, count=1, flags=re.DOTALL)
        return result.strip()

    def import_issue_as_task(self, issue_id_or_url: Union[str, int],
                           task_priority: str = "m", update_mode: bool = False) -> str:
        """Import a GitHub issue as a new Claude task or update existing task."""
        try:
            # Parse URL if provided
            issue_id = issue_id_or_url
            if isinstance(issue_id_or_url, str) and ('github.com' in issue_id_or_url or '/issues/' in issue_id_or_url):
                parsed = self.github.parse_issue_url(issue_id_or_url)
                if parsed:
                    issue_id = parsed['issue_id']
                else:
                    raise Exception(f"Could not parse GitHub issue URL: {issue_id_or_url}")

            # Fetch issue
            issue = self.github.get_issue(issue_id)
            if not issue:
                raise Exception(f"GitHub issue #{issue_id} not found")

            # Generate task name (shared canonical slugifier)
            task_name_base = self._slugify(issue['title'])
            task_name = f"{task_priority}-{task_name_base}"

            # Find or create task file
            existing_task_file = find_task_file(self.project_root, task_name)
            if existing_task_file and not update_mode:
                raise Exception(f"Task file already exists: {existing_task_file}. Use update_mode=True to update it.")

            task_file = existing_task_file if existing_task_file else (self.project_root / 'team-management' / 'tasks' / f"{task_name}.md")

            # Format description
            raw_description = issue.get('body', 'No description provided.')
            description = self._strip_yaml_frontmatter(raw_description)
            labels = [label['name'] for label in issue.get('labels', [])]

            # Create task content
            task_content = f"""---
task: {task_name}
branch: feature/{task_name_base}
status: pending
created: {datetime.now().strftime('%Y-%m-%d')}
github_issue: {issue_id}
---

# {issue['title']}

## Problem/Goal

{description}

## Success Criteria

- [ ] Implement solution as described in GitHub issue #{issue_id}

## GitHub Issue Details

- **Issue**: #{issue_id}
- **URL**: {issue['html_url']}
- **State**: {issue['state']}
- **Labels**: {', '.join(labels) if labels else 'none'}
- **Created**: {issue['created_at']}

## Work Log

- [{datetime.now().strftime('%Y-%m-%d')}] Task {"updated from" if update_mode else "created from"} GitHub issue #{issue_id}
"""

            # Write task file
            task_file.parent.mkdir(parents=True, exist_ok=True)
            with open(task_file, 'w', encoding='utf-8') as f:
                f.write(task_content)

            # Link task to issue
            self.link_task_to_issue(task_name, issue_id)

            return task_name

        except Exception as e:
            raise Exception(f"Failed to import GitHub issue: {e}")

    def create_issue_from_task(self, task_name: str) -> Optional[Union[str, int]]:
        """Create a GitHub issue from a Claude task."""
        try:
            task_file = find_task_file(self.project_root, task_name)
            if not task_file:
                raise Exception(f"Task file not found: {task_name}.md")

            with open(task_file, 'r', encoding='utf-8') as f:
                task_content = f.read()

            # Extract title (shared helper)
            title = self._extract_task_title(task_content, task_name)

            # Create issue
            description = task_content + '\n\n---\n\n*Generated by team-management*'
            issue = self.github.create_issue(
                title=title,
                description=description,
                labels=self.github.default_labels
            )

            if not issue:
                raise Exception("Failed to create GitHub issue")

            issue_id = issue['number']
            self.link_task_to_issue(task_name, issue_id)

            return issue_id

        except Exception as e:
            raise Exception(f"Failed to create GitHub issue from task: {e}")

    def sync_task_status_to_issue(self, task_name: str, status: str, additional_info: Dict = None):
        """Sync task status to the linked GitHub issue."""
        try:
            mapping = self.get_task_mapping(task_name)
            issue_id = mapping.get('issue_id') if mapping else None
            if not issue_id:
                return

            # Status mapping
            status_map = {
                'completed': 'closed',
                'blocked': None,
                'in-progress': 'open',
                'pending': 'open'
            }

            github_state = status_map.get(status)

            # Update issue state
            if github_state:
                self.github.update_issue(issue_id, status=github_state)

            # Add workflow label
            workflow_label = self.github.workflow_labels.get(status)
            if workflow_label:
                issue = self.github.get_issue(issue_id)
                current_labels = [label['name'] for label in issue.get('labels', [])]

                if workflow_label not in current_labels:
                    filtered_labels = [
                        l for l in current_labels
                        if l not in self.github.workflow_labels.values()
                    ]
                    filtered_labels.append(workflow_label)
                    self.github.update_issue(issue_id, labels=filtered_labels)

            # Add comment
            comment_parts = [f"**Task Status Update**: `{status}`"]
            if additional_info:
                if additional_info.get('branch'):
                    comment_parts.append(f"**Branch**: `{additional_info['branch']}`")
                if additional_info.get('message'):
                    comment_parts.append(f"\n{additional_info['message']}")

            comment = "\n".join(comment_parts)
            comment += "\n\n---\nAutomated update from [Claude Code](https://claude.com/claude-code)"

            self.github.add_comment(issue_id, comment)

            # Update mapping timestamp
            def _mut(mappings):
                if task_name in mappings:
                    mappings[task_name]['last_synced'] = datetime.now().isoformat()
                    mappings[task_name]['sync_count'] = mappings[task_name].get('sync_count', 0) + 1
                    return True
                return False
            self._locked_mapping_update(_mut)

        except Exception as e:
            # Was a bare `except Exception: pass` — sync failures on the live
            # auto-sync path (post-tool-use, protocol completion) were invisible.
            _log(f"github sync_task_status_to_issue failed for '{task_name}': {e}")

    def update_issue_description_from_task(self, task_name: str) -> bool:
        """Update GitHub/Gitea issue description with current task content."""
        mapping = self.get_task_mapping(task_name)
        issue_id = mapping.get('issue_id') if mapping else None
        if not issue_id:
            raise Exception(f"Task {task_name} not linked to GitHub/Gitea issue")

        task_file = find_task_file(self.project_root, task_name)
        if not task_file:
            raise Exception(f"Task file not found: {task_name}.md")

        with open(task_file, 'r', encoding='utf-8') as f:
            task_content = f.read()

        # Extract title (shared helper)
        title = self._extract_task_title(task_content, task_name)

        # Update issue
        enhanced_description = task_content + '\n\n---\n*Updated by team-management after analysis and discussion*'
        result = self.github.update_issue(issue_id, title=title, description=enhanced_description)

        if result:
            def _mut(mappings):
                if task_name in mappings:
                    mappings[task_name]['last_synced'] = datetime.now().isoformat()
                    mappings[task_name]['description_updated'] = datetime.now().isoformat()
                    return True
                return False
            self._locked_mapping_update(_mut)
            return True

        return False

    def _add_code_review_comment_to_pr(self, pr_number: int, task_name: str) -> bool:
        """Add code review results as a comment to the pull request."""
        try:
            code_review = self._extract_code_review_results(task_name)
            if not code_review:
                return False

            mapping = self.get_task_mapping(task_name)
            issue_id = mapping.get('issue_id') if mapping else None
            if issue_id:
                result = self.github.add_comment(issue_id, code_review)
                return result is not None

            return False
        except Exception:
            return False

    def create_pull_request_from_task(self, task_name: str, source_branch: str,
                                     target_branch: str = None, commit_body: str = None) -> Optional[Dict]:
        """Create a GitHub Pull Request from a task.

        Dual-path mirroring plugin/hooks/gitlab/task_sync.py:
        - If the task is linked to an issue (mapping has issue_id), the PR body
          references the issue via "Closes #<id>".
        - Otherwise (no mapping, or mapping without issue_id — e.g. when
          github.issue_tracking_enabled is false), a standalone PR is created.
        """
        from .pr_manager import GitHubPRManager

        try:
            if target_branch is None:
                target_branch = self.github.get_default_branch()

            if source_branch == target_branch:
                raise Exception(f"Cannot create PR: source and target branches are the same ({source_branch})")

            mapping = self.get_task_mapping(task_name)
            issue_id = mapping.get('issue_id') if mapping else None

            task_file = find_task_file(self.project_root, task_name)
            if not task_file:
                raise Exception(f"Task file not found: {task_name}.md")

            with open(task_file, 'r', encoding='utf-8') as f:
                content = f.read()

            title_match = re.search(r'^# (.+)$', content, re.MULTILINE)
            title = title_match.group(1) if title_match else task_name

            pr_mgr = GitHubPRManager(self.github)
            pr_body = pr_mgr.generate_enhanced_pr_description(issue_id, source_branch, target_branch, task_name, commit_body)

            pr = pr_mgr.create_pull_request(
                title=f"Resolve: {title}",
                body=pr_body,
                head=source_branch,
                base=target_branch
            )

            if pr:
                def _mut(mappings):
                    if task_name not in mappings:
                        mappings[task_name] = {}
                    mappings[task_name]['pull_request_number'] = pr.get('number')
                    mappings[task_name]['pull_request_url'] = pr.get('html_url', '')
                    mappings[task_name]['last_synced'] = datetime.now().isoformat()
                    return True
                self._locked_mapping_update(_mut)

            return pr

        except Exception as e:
            raise Exception(f"Failed to create pull request: {e}")
