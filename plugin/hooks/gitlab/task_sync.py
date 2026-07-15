#!/usr/bin/env python3
"""
GitLab task synchronization.

Handles bidirectional sync between Claude tasks and GitLab issues.
"""
import json
from datetime import datetime
from typing import Dict, List, Optional, Union

from issue_provider_base import IssueTrackingTaskSync, find_task_file


from shared_state import get_provider_logger

_log = get_provider_logger('gitlab.log')


class GitLabTaskSync(IssueTrackingTaskSync):
    """Synchronization utilities between Claude tasks and GitLab issues."""

    def __init__(self):
        """Initialize with GitLab API provider"""
        from .api import GitLabAPI
        self.gitlab = GitLabAPI()
        super().__init__(self.gitlab)  # Sets up mappings file via base class

    def link_task_to_issue(self, task_name: str, issue_id: int, sync_direction: str = "bidirectional"):
        """Create a mapping between a task and GitLab issue."""
        # Fetch issue details
        issue = self.gitlab.get_issue(issue_id)
        if not issue:
            raise Exception(f"Could not fetch GitLab issue {issue_id}")

        def _mut(mappings):
            mappings[task_name] = {
                'gitlab_issue_id': issue['id'],
                'gitlab_issue_iid': issue['iid'],
                'gitlab_url': issue['web_url'],
                'task_file': f'team-management/tasks/{task_name}.md',
                'sync_direction': sync_direction,
                'created': issue['created_at'],
                'last_synced': datetime.now().isoformat(),
                'status': 'pending'
            }
            return True
        self._locked_mapping_update(_mut)

    def import_issue_as_task(self, issue_id_or_url: Union[str, int], task_priority: str = "m",
                             update_mode: bool = False) -> str:
        """Import a GitLab issue as a new Claude task (or update an existing one)."""
        from . import parse_gitlab_issue_url

        # Handle URL input
        if isinstance(issue_id_or_url, str) and issue_id_or_url.startswith('http'):
            url_parts = parse_gitlab_issue_url(issue_id_or_url)
            if not url_parts:
                raise Exception(f"Invalid GitLab issue URL: {issue_id_or_url}")
            issue_id = url_parts['issue_id']
        else:
            issue_id = issue_id_or_url

        issue = self.gitlab.get_issue(issue_id)
        if not issue:
            raise Exception(f"Could not fetch GitLab issue {issue_id}")

        # Generate task name from issue title (shared canonical slugifier)
        issue_title = issue['title']
        task_name = f"{task_priority}-{self._slugify(issue_title)}"

        # Check if task file already exists
        existing_task_file = find_task_file(self.project_root, task_name)
        if existing_task_file and not update_mode:
            raise Exception(f"Task file already exists: {existing_task_file}. Use update_mode=True to update it.")

        # Create/locate task file (reuse the existing path when updating)
        task_file = existing_task_file if existing_task_file else (
            self.project_root / 'team-management' / 'tasks' / f"{task_name}.md"
        )

        # Convert GitLab labels to task modules
        labels = issue.get('labels', [])
        if labels and isinstance(labels[0], dict):
            modules = [label['name'] for label in labels]
        else:
            modules = labels if labels else ['imported']

        # Create task content
        task_content = f"""---
task: {task_name}
branch: feature/{task_name}
status: pending
created: {datetime.now().strftime('%Y-%m-%d')}
modules: {json.dumps(modules)}
---

# {issue_title}

## Problem/Goal
{issue['description'] or 'Imported from GitLab issue'}

## Success Criteria
- [ ] Complete the requirements from GitLab issue #{issue['iid']}

## Context Files
<!-- Add relevant context files as you work -->

## User Notes
**Imported from GitLab Issue:**
- Issue ID: #{issue['iid']}
- GitLab URL: {issue['web_url']}
- Created: {issue['created_at']}
- Author: {issue['author']['name']}

## Work Log
- [{datetime.now().strftime('%Y-%m-%d')}] Imported from GitLab issue #{issue['iid']}
"""

        with open(task_file, 'w', encoding='utf-8') as f:
            f.write(task_content)

        # Create mapping
        def _mut(mappings):
            mappings[task_name] = {
                'gitlab_issue_id': issue['id'],
                'gitlab_issue_iid': issue['iid'],
                'gitlab_url': issue['web_url'],
                'task_file': f'team-management/tasks/{task_name}.md',
                'sync_direction': 'bidirectional',
                'created': issue['created_at'],
                'last_synced': datetime.now().isoformat(),
                'status': 'pending'
            }
            return True
        self._locked_mapping_update(_mut)

        return task_name

    def create_gitlab_issue_from_task(self, task_name: str) -> Optional[int]:
        """Create a GitLab issue from a Claude task."""
        task_file = find_task_file(self.project_root, task_name)
        if not task_file:
            raise Exception(f"Task file not found: {task_name}.md")

        # Parse task file
        with open(task_file, 'r', encoding='utf-8') as f:
            task_content = f.read()

        # Extract title from task content (shared helper)
        title = self._extract_task_title(task_content, task_name)

        # Create issue
        description = task_content + '\n\n---\n*Generated by team-management*'
        issue = self.gitlab.create_issue(title, description)

        if issue:
            # Save mapping
            def _mut(mappings):
                mappings[task_name] = {
                    'gitlab_issue_id': issue['id'],
                    'gitlab_issue_iid': issue['iid'],
                    'gitlab_url': issue['web_url'],
                    'task_file': f'team-management/tasks/{task_name}.md',
                    'sync_direction': 'bidirectional',
                    'created': issue['created_at'],
                    'last_synced': issue['created_at'],
                    'status': 'pending'
                }
                return True
            self._locked_mapping_update(_mut)
            # Surface the project-scoped iid (the user-facing number and the one
            # every GitLab REST consumer puts in /projects/X/issues/{iid}). The
            # global `id` is still stored in the mapping above but must never be
            # the round-trip handle -- returning it 404s issue_set_status/comment.
            return issue['iid']

        return None

    def create_issue_from_task(self, task_name: str) -> Optional[int]:
        """Alias for create_gitlab_issue_from_task()."""
        return self.create_gitlab_issue_from_task(task_name)

    def _add_code_review_comment_to_mr(self, mr_iid: Union[str, int], task_name: str) -> bool:
        """Add code review results as MR comment."""
        try:
            code_review = self._extract_code_review_results(task_name)
            if not code_review:
                return False

            from .mr_manager import GitLabMRManager
            mr_mgr = GitLabMRManager(self.gitlab)
            result = mr_mgr.add_mr_comment(mr_iid, code_review)
            return result is not None
        except Exception:
            return False

    def update_issue_description_from_task(self, task_name: str) -> bool:
        """Update GitLab issue description with current task content."""
        mapping = self.get_task_mapping(task_name)
        # MR-only mappings are truthy but lack gitlab_issue_iid; treat as not linked
        issue_iid = mapping.get('gitlab_issue_iid') if mapping else None
        if not issue_iid:
            raise Exception(f"Task {task_name} not linked to GitLab issue")

        task_file = find_task_file(self.project_root, task_name)
        if not task_file:
            raise Exception(f"Task file not found: {task_name}.md")

        with open(task_file, 'r', encoding='utf-8') as f:
            task_content = f.read()

        # Extract title (shared helper)
        title = self._extract_task_title(task_content, task_name)

        # Update issue
        enhanced_description = task_content + '\n\n---\n*Updated by team-management*'
        result = self.gitlab.update_issue(issue_iid, title=title, description=enhanced_description)

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

    def sync_task_status_to_issue(self, task_name: str, status: str, additional_info: Dict = None):
        """Sync task status to GitLab issue (base class method)."""
        return self.sync_task_status_to_gitlab(task_name, status, additional_info)

    def sync_task_status_to_gitlab(self, task_name: str, status: str, merge_request_info: Dict = None):
        """Sync task status and content to GitLab issue."""
        mapping = self.get_task_mapping(task_name)
        # MR-only mappings are truthy but lack gitlab_issue_iid; nothing to sync
        issue_iid = mapping.get('gitlab_issue_iid') if mapping else None
        if not issue_iid:
            return

        # 1. Update issue description
        try:
            self.update_issue_description_from_task(task_name)
        except Exception as e:
            _log(f"Warning: Failed to update issue description: {e}")

        # Map task statuses to GitLab actions
        status_map = {
            'completed': 'close',
            'blocked': None,
            'in-progress': 'reopen',
            'pending': 'reopen'
        }

        state_event = status_map.get(status)

        # 2. Add status update comment
        if status == 'completed':
            comment = self._generate_rich_completion_comment(task_name, merge_request_info)
        else:
            comment = f"**Task Status Update from Claude Code**: {status}"

        self.gitlab.add_issue_comment(issue_iid, comment)

        # 3. Update issue state if needed
        if state_event:
            self.gitlab.update_issue(issue_iid, state_event=state_event)

        # Update last synced time
        def _mut(mappings):
            if task_name in mappings:
                mappings[task_name]['last_synced'] = datetime.now().isoformat()
                return True
            return False
        self._locked_mapping_update(_mut)

    def _extract_labels_from_task(self, task_name: str) -> List[str]:
        """Extract labels from task file frontmatter."""
        task_file = find_task_file(self.project_root, task_name)
        if not task_file:
            return []

        try:
            with open(task_file, 'r', encoding='utf-8') as f:
                content = f.read()

            lines = content.split('\n')
            for line in lines[:10]:
                if line.strip().startswith('modules:'):
                    modules_str = line.split('modules:', 1)[1].strip()
                    try:
                        modules = json.loads(modules_str)
                        return modules if isinstance(modules, list) else []
                    except (ValueError, TypeError):
                        pass
        except (OSError, ValueError):
            pass

        return []

    def create_merge_request_for_task(self, task_name: str, source_branch: str = None,
                                     target_branch: str = None, title: str = None,
                                     description: str = None, labels: List[str] = None,
                                     commit_body: str = None) -> Optional[int]:
        """Create GitLab merge request for a task."""
        import subprocess
        from .mr_manager import GitLabMRManager

        # Use auto-detected default branch if not specified
        if target_branch is None:
            target_branch = self.gitlab.default_branch

        # Get current branch if not specified
        if not source_branch:
            try:
                result = subprocess.run(['git', 'branch', '--show-current'],
                                     stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                     cwd=self.project_root, timeout=5)
                source_branch = result.stdout.strip()
            except (subprocess.SubprocessError, OSError):
                raise Exception("Could not determine current branch")

        if not source_branch or source_branch == target_branch:
            raise Exception(f"Invalid source branch: {source_branch}")

        # Check if task is linked to GitLab issue
        mapping = self.get_task_mapping(task_name)
        mr_mgr = GitLabMRManager(self.gitlab)

        if mapping and 'gitlab_issue_iid' in mapping:
            # Issue-linked workflow
            issue_iid = mapping['gitlab_issue_iid']
            mr = mr_mgr.create_merge_request(issue_iid, source_branch, target_branch,
                                            task_name=task_name, labels=labels, commit_body=commit_body)
        else:
            # Standalone workflow
            if not title:
                title = f"Task: {task_name.replace('-', ' ').title()}"

            if not description:
                description = mr_mgr.generate_enhanced_mr_description(None, source_branch, target_branch, task_name)

            # Determine labels
            if labels is None:
                mr_labels = list(self.gitlab.default_labels)
                task_modules = self._extract_labels_from_task(task_name)
                for module in task_modules:
                    if module not in mr_labels:
                        mr_labels.append(module)
            else:
                mr_labels = labels

            # Create MR directly
            data = {
                'source_branch': source_branch,
                'target_branch': target_branch,
                'title': title,
                'description': description,
                'labels': ','.join(mr_labels) if mr_labels else '',
                'remove_source_branch': True,
                'squash': True
            }

            mr = self.gitlab._make_request('POST', f"/projects/{self.gitlab.project_path}/merge_requests", data)

        if mr:
            # Store MR mapping
            def _mut(mappings):
                if task_name not in mappings:
                    mappings[task_name] = {}
                mappings[task_name]['merge_request_id'] = mr['id']
                mappings[task_name]['merge_request_iid'] = mr['iid']
                mappings[task_name]['merge_request_url'] = mr.get('web_url', '')
                mappings[task_name]['last_synced'] = datetime.now().isoformat()
                return True
            self._locked_mapping_update(_mut)

            return mr['iid']

        return None
