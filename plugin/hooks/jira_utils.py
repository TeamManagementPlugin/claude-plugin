#!/usr/bin/env python3
"""
Jira API utilities for team-management integration.

Provides comprehensive Jira API integration using Personal Access Tokens
for self-hosted Jira instances. Handles bidirectional synchronization
between Claude tasks and Jira issues.

Key features:
    - Personal Access Token authentication for self-hosted Jira
    - Jira REST API v2 and v3 support with automatic format conversion
    - Support for standard Jira issue types (Bug, Epic, Story, Sub-task, Task)
    - Provider-prefixed issue IDs (jira:PROJ-123)
    - Robust project root detection and configuration management
    - Full task-to-issue synchronization
    - Markdown-to-Jira-Wiki conversion for descriptions and comments
    - Structured error reporting with detailed HTTP context

Error Handling:
    All API methods use structured error responses from _make_request():
    - Returns error dict on failure with error_type, status_code, message,
      response_body, url, and method
    - API methods check for error responses and raise exceptions with full context
    - Error types: http_error, timeout, connection_error, request_error, unexpected_error
    - Exception messages include HTTP status codes and response bodies for debugging

Configuration:
    Uses config.json with jira.api_token (Personal Access Token)
    instead of API keys, suitable for self-hosted Jira instances.

Security:
    - Comprehensive input validation for all API parameters
    - Secure URL encoding prevents injection attacks
    - No sensitive token exposure in error messages

See Also:
    - .claude/issue-tracking/CLAUDE.md: Complete module documentation
    - team-management/config.json: Configuration examples
"""

import json
import re
from datetime import datetime
from typing import Dict, List, Optional, Union

from issue_provider_base import IssueTrackingProvider, IssueTrackingTaskSync, find_task_file
from shared_state import resolve_provider_token, get_provider_logger

_log = get_provider_logger('jira.log')


class JiraProvider(IssueTrackingProvider):
    """Jira API provider for team-management."""
    
    @property
    def provider_name(self) -> str:
        return "jira"
    
    def __init__(self):
        """Initialize Jira API with config from config.json"""
        super().__init__()
        
        if not self.config or not self.config.get('jira', {}).get('enabled'):
            raise Exception("Jira integration not enabled in config.json")
        
        jira_config = self.config['jira']
        self.api_token = resolve_provider_token('jira', jira_config.get('api_token'))  # Personal Access Token
        self.base_url = jira_config.get('base_url', 'https://your-jira-instance.com')
        self.project_key = jira_config.get('project_key', 'PROJ')
        self.api_version = jira_config.get('api_version', '2')  # Default to v2 for compatibility
        self.default_issue_type = jira_config.get('default_issue_type', 'Task')
        self.supported_types = jira_config.get('supported_issue_types', 
                                             ['Bug', 'Epic', 'Story', 'Sub-task', 'Task'])
        
        if not self.api_token:
            raise Exception(
                "Jira API token (Personal Access Token) not found. Set it in "
                ".claude/state/provider-tokens.json (key: jira) — a per-project "
                "file the AI cannot read — or jira.api_token in config.json.")
        if not self.project_key:
            raise Exception("Jira project key not found in config.json")
        if self.api_version not in ['2', '3']:
            raise Exception(f"Unsupported Jira API version: {self.api_version}. Supported versions: 2, 3")
            
        # Remove trailing slash from base_url and setup API endpoints
        self.base_url = self.base_url.rstrip('/')
        self._warn_if_insecure_base_url(self.base_url)
        self.api_base = f"{self.base_url}/rest/api/{self.api_version}"
        # Per-request timeout consumed by the shared base _make_request
        self.request_timeout = 30
        
        # Setup headers for API calls - Personal Access Token uses Bearer auth
        self.headers = {
            'Authorization': f'Bearer {self.api_token}',
            'Content-Type': 'application/json'
        }
    
    def _markdown_to_jira_wiki(self, text: str) -> str:
        """Convert Markdown text to Jira Wiki Markup format"""
        import re
        
        # Use placeholder system to protect code content from other conversions
        code_blocks = {}
        inline_codes = {}
        block_counter = 0
        inline_counter = 0
        
        # STEP 1: Extract and protect code blocks with placeholders
        def replace_code_block(match):
            nonlocal block_counter
            placeholder = f"__CODE_BLOCK_{block_counter}__"
            code_blocks[placeholder] = "{code}" + match.group(1) + "{code}"
            block_counter += 1
            return placeholder
        
        text = re.sub(r'```([^`]*?)```', replace_code_block, text, flags=re.DOTALL)
        
        # STEP 2: Extract and protect inline code with placeholders
        def replace_inline_code(match):
            nonlocal inline_counter
            placeholder = f"__INLINE_CODE_{inline_counter}__"
            # Correct Jira inline code syntax is double braces
            inline_codes[placeholder] = "{{" + match.group(1) + "}}"
            inline_counter += 1
            return placeholder
        
        text = re.sub(r'`([^`]+?)`', replace_inline_code, text)
        
        # STEP 3: Now safely process other formatting (code is protected)
        # Convert headers (# → h1., ## → h2., etc.)
        text = re.sub(r'^### (.+)$', r'h3. \1', text, flags=re.MULTILINE)
        text = re.sub(r'^## (.+)$', r'h2. \1', text, flags=re.MULTILINE)
        text = re.sub(r'^# (.+)$', r'h1. \1', text, flags=re.MULTILINE)
        
        # Convert bold first (**text** → *text*) and mark with special chars to protect
        text = re.sub(r'\*\*(.+?)\*\*', r'__BOLD_START__\1__BOLD_END__', text)
        
        # Convert italic (*text* → _text*) - now safe from bold interference
        text = re.sub(r'\*([^*]+?)\*', r'_\1_', text)
        
        # Restore bold with correct syntax
        text = re.sub(r'__BOLD_START__(.+?)__BOLD_END__', r'*\1*', text)
        
        # Convert checkboxes (- [x] → * (x) checked, - [ ] → * ( ) unchecked)
        # Note: Jira Wiki doesn't support checkboxes natively, using closest representation
        text = re.sub(r'^- \[x\] (.+)$', r'* (x) \1', text, flags=re.MULTILINE | re.IGNORECASE)
        text = re.sub(r'^- \[ \] (.+)$', r'* ( ) \1', text, flags=re.MULTILINE)

        # Convert bullet points (- → *)
        text = re.sub(r'^- (.+)$', r'* \1', text, flags=re.MULTILINE)
        
        # Convert numbered lists (1. → #)  
        text = re.sub(r'^\d+\. (.+)$', r'# \1', text, flags=re.MULTILINE)
        
        # Convert links ([text](url) → [text|url])
        text = re.sub(r'\[([^\]]+?)\]\(([^)]+?)\)', r'[\1|\2]', text)
        
        # Convert blockquotes (> text → bq. text)
        text = re.sub(r'^> (.+)$', r'bq. \1', text, flags=re.MULTILINE)
        
        # Convert horizontal rules (--- → ----)
        text = re.sub(r'^---+$', '----', text, flags=re.MULTILINE)
        
        # STEP 4: Restore protected code content
        for placeholder, code_content in code_blocks.items():
            text = text.replace(placeholder, code_content)
        
        for placeholder, code_content in inline_codes.items():
            text = text.replace(placeholder, code_content)
        
        return text
    
    def _format_description(self, text: str) -> Union[str, Dict]:
        """Format description based on API version"""
        if self.api_version == '3':
            # Document format for v3
            return {
                'type': 'doc',
                'version': 1,
                'content': [
                    {
                        'type': 'paragraph',
                        'content': [
                            {
                                'type': 'text',
                                'text': text
                            }
                        ]
                    }
                ]
            }
        else:
            # Convert Markdown to Jira Wiki Markup for v2
            return self._markdown_to_jira_wiki(text)
    
    def _format_comment(self, text: str) -> Dict:
        """Format comment based on API version"""
        if self.api_version == '3':
            return {
                'body': {
                    'type': 'doc',
                    'version': 1,
                    'content': [
                        {
                            'type': 'paragraph',
                            'content': [
                                {
                                    'type': 'text',
                                    'text': text
                                }
                            ]
                        }
                    ]
                }
            }
        else:
            # Convert Markdown to Jira Wiki Markup for v2
            return {'body': self._markdown_to_jira_wiki(text)}
    
    def get_issue(self, issue_id: Union[str, int]) -> Optional[Dict]:
        """Get a specific Jira issue by ID or key"""
        # Validate input - Jira accepts both numeric IDs and project keys like PROJ-123
        if isinstance(issue_id, str):
            if not (issue_id.isdigit() or re.match(r'^[A-Z]+-\d+$', issue_id)):
                raise ValueError(f"Invalid Jira issue ID format: {issue_id}")

        result = self._make_request('GET', f"/issue/{issue_id}")

        # Check for error response and raise exception with details
        return self._raise_on_error(result, "Jira API error")
    
    def create_issue(self, title: str, description: str = "", labels: List[str] = None,
                    issue_type: str = None) -> Optional[Dict]:
        """Create a new Jira issue"""
        # Input validation
        if not title or len(title.strip()) == 0:
            raise ValueError("Issue title cannot be empty")
        if len(title) > 255:
            raise ValueError("Issue title too long (max 255 characters)")

        if issue_type is None:
            issue_type = self.default_issue_type

        if issue_type not in self.supported_types:
            raise ValueError(f"Unsupported issue type: {issue_type}. Supported: {self.supported_types}")

        # API version-aware issue creation format
        fields = {
            'project': {
                'key': self.project_key
            },
            'summary': title.strip(),
            'description': self._format_description(description),
            'issuetype': {
                'name': issue_type
            }
        }

        # Add labels if provided
        if labels:
            fields['labels'] = labels

        data = {'fields': fields}
        result = self._make_request('POST', "/issue", data)

        # Check for error response and raise exception with details
        return self._raise_on_error(result, "Jira API error creating issue")
    
    def update_issue(self, issue_id: Union[str, int], title: str = None,
                    description: str = None, status: str = None,
                    labels: List[str] = None) -> Optional[Dict]:
        """Update an existing Jira issue"""
        fields = {}

        if title is not None:
            fields['summary'] = title

        if description is not None:
            fields['description'] = self._format_description(description)

        if labels is not None:
            fields['labels'] = labels

        data = {'fields': fields} if fields else {}

        # Handle status transitions separately if needed
        if status:
            # For status changes, we need to use the transitions endpoint
            # This is a simplified implementation - real Jira workflows are more complex
            transitions_url = f"/issue/{issue_id}/transitions"
            transitions_response = self._make_request('GET', transitions_url)

            # Check for error in transitions response
            self._raise_on_error(transitions_response, "Jira API error getting transitions")

            if transitions_response:
                # Find appropriate transition for the status
                for transition in transitions_response.get('transitions', []):
                    if transition.get('to', {}).get('name', '').lower() == status.lower():
                        transition_data = {
                            'transition': {
                                'id': transition['id']
                            }
                        }
                        trans_result = self._make_request('POST', transitions_url, transition_data)
                        # Check for error in transition
                        self._raise_on_error(trans_result, "Jira API error updating status")
                        break
                else:
                    # No transition leads to the requested status — surface it
                    # instead of silently leaving the issue state unchanged.
                    _log(
                        f"[jira_utils] Warning: no Jira transition to status "
                        f"'{status}' found for issue {issue_id}; status not changed"
                    )

        if data.get('fields'):
            result = self._make_request('PUT', f"/issue/{issue_id}", data)
            # Check for error response
            return self._raise_on_error(result, "Jira API error updating issue")

        return {'updated': True}
    
    def add_comment(self, issue_id: Union[str, int], comment: str) -> Optional[Dict]:
        """Add a comment to a Jira issue"""
        # API version-aware comment format
        data = self._format_comment(comment)

        result = self._make_request('POST', f"/issue/{issue_id}/comment", data)

        # Check for error response and raise exception with details
        return self._raise_on_error(result, "Jira API error adding comment")
    
    def parse_issue_url(self, url: str) -> Optional[Dict[str, str]]:
        """Parse Jira issue URL to extract components
        
        Args:
            url: Jira issue URL like https://your-jira.com/browse/PROJ-123
            
        Returns:
            Dict with base_url, project_key, issue_key keys, or None if invalid
        """
        # Pattern to match Jira issue URLs
        # Supports both cloud and self-hosted instances
        # Common formats: /browse/PROJ-123, /projects/PROJ/issues/PROJ-123
        patterns = [
            r'^(https?://[^/]+)/browse/([A-Z]+-\d+)(?:[/#?].*)?$',
            r'^(https?://[^/]+)/projects/[^/]+/issues/([A-Z]+-\d+)(?:[/#?].*)?$'
        ]
        
        for pattern in patterns:
            match = re.match(pattern, url.strip())
            if match:
                base_url, issue_key = match.groups()
                project_key = issue_key.split('-')[0]
                
                return {
                    'base_url': base_url,
                    'project_key': project_key,
                    'issue_key': issue_key
                }
        
        return None
    
    def format_issue_id(self, issue_id: Union[str, int]) -> str:
        """Format issue ID with Jira provider prefix"""
        # If it's already a Jira key (PROJ-123), use it directly
        if isinstance(issue_id, str) and re.match(r'^[A-Z]+-\d+$', issue_id):
            return f"jira:{issue_id}"
        # Otherwise, assume it's a numeric ID and format it
        return f"jira:{issue_id}"
    
    def extract_issue_id(self, formatted_id: str) -> Union[str, int]:
        """Extract the actual issue ID from a provider-prefixed ID"""
        if formatted_id.startswith('jira:'):
            return formatted_id[5:]  # Remove 'jira:' prefix
        return formatted_id
    
    def get_supported_issue_types(self) -> List[str]:
        """Get list of supported issue types for this provider"""
        return self.supported_types.copy()
    
    def validate_configuration(self) -> bool:
        """Validate that the Jira provider is properly configured"""
        try:
            # Test basic API access
            user_info = self._make_request('GET', '/myself')
            if not user_info:
                return False
            
            # Test project access
            project_info = self._make_request('GET', f'/project/{self.project_key}')
            if not project_info:
                return False
            
            return True
        except Exception:
            return False


class JiraTaskSync(IssueTrackingTaskSync):
    """Synchronization utilities between Claude tasks and Jira issues."""
    
    def __init__(self):
        """Initialize with Jira provider"""
        provider = JiraProvider()
        super().__init__(provider)
        self.jira = provider
    
    def link_task_to_issue(self, task_name: str, issue_id: Union[str, int], 
                          sync_direction: str = "bidirectional") -> bool:
        """Create a mapping between a task and Jira issue"""
        try:
            def _mut(mappings):
                mappings[task_name] = {
                    'jira_issue_id': str(issue_id),
                    'jira_issue_key': str(issue_id) if isinstance(issue_id, str) and '-' in str(issue_id) else None,
                    'last_synced': datetime.now().isoformat(),
                    'sync_direction': sync_direction
                }
                return True
            self._locked_mapping_update(_mut)
            return True
        except Exception as e:
            _log(f"Failed to link task to Jira issue: {e}")
            return False
    
    def import_issue_as_task(self, issue_id_or_url: Union[str, int], 
                           task_priority: str = "m", update_mode: bool = False) -> str:
        """Import a Jira issue as a new Claude task (or update an existing one)"""
        # Handle URL input
        if isinstance(issue_id_or_url, str) and issue_id_or_url.startswith('http'):
            url_parts = self.jira.parse_issue_url(issue_id_or_url)
            if not url_parts:
                raise Exception(f"Invalid Jira issue URL: {issue_id_or_url}")
            
            issue_id = url_parts['issue_key']
        else:
            issue_id = issue_id_or_url
        
        issue = self.jira.get_issue(issue_id)
        if not issue:
            raise Exception(f"Could not fetch Jira issue {issue_id}")
        
        # Generate task name from issue summary (shared canonical slugifier)
        issue_summary = issue['fields']['summary']
        task_name = f"{task_priority}-{self._slugify(issue_summary)}"
        
        # Check if task file already exists
        existing_task_file = find_task_file(self.project_root, task_name)
        if existing_task_file and not update_mode:
            raise Exception(f"Task file already exists: {existing_task_file}. Use update_mode=True to update it.")

        # Create/locate task file (reuse the existing path when updating)
        task_file = existing_task_file if existing_task_file else (
            self.project_root / 'team-management' / 'tasks' / f"{task_name}.md"
        )

        # Extract description (handle Jira's document format)
        description = "Imported from Jira issue"
        if issue['fields'].get('description'):
            desc_content = issue['fields']['description']
            if isinstance(desc_content, dict) and desc_content.get('content'):
                # Extract text from Jira's document format
                text_parts = []
                for content_item in desc_content['content']:
                    if content_item.get('type') == 'paragraph' and content_item.get('content'):
                        for text_item in content_item['content']:
                            if text_item.get('type') == 'text':
                                text_parts.append(text_item.get('text', ''))
                description = ' '.join(text_parts) if text_parts else description
        
        # Get issue type and status
        issue_type = issue['fields']['issuetype']['name']
        issue_status = issue['fields']['status']['name']
        
        # Create task content
        task_content = f"""---
task: {task_name}
branch: feature/{task_name}
status: pending
created: {datetime.now().strftime('%Y-%m-%d')}
modules: [jira-import]
---

# {issue_summary}

## Problem/Goal
{description}

## Success Criteria
- [ ] Complete the requirements from Jira issue {issue['key']}

## Context Files
<!-- Add relevant context files as you work -->

## User Notes
**Imported from Jira Issue:**
- Issue Key: {issue['key']}
- Issue Type: {issue_type}
- Status: {issue_status}
- Created: {issue['fields']['created']}
- Reporter: {issue['fields']['reporter']['displayName']}

## Work Log
- [{datetime.now().strftime('%Y-%m-%d')}] Imported from Jira issue {issue['key']}
"""
        
        with open(task_file, 'w', encoding='utf-8') as f:
            f.write(task_content)
        
        # Create mapping
        self.link_task_to_issue(task_name, issue['key'])
        
        return task_name
    
    def create_issue_from_task(self, task_name: str) -> Optional[str]:
        """Create a Jira issue from a Claude task"""
        task_file = find_task_file(self.project_root, task_name)
        if not task_file:
            raise Exception(f"Task file not found: {task_name}.md")
        
        # Parse task file
        with open(task_file, 'r', encoding='utf-8') as f:
            task_content = f.read()
        
        # Extract title from task content (shared helper)
        title = self._extract_task_title(task_content, "Claude Code Task")
        
        # Create Jira issue with task content as description
        description = task_content + '\n\n---\nGenerated by team-management'
        
        # Determine issue type based on task name
        issue_type = self.jira.default_issue_type
        if 'bug' in task_name.lower() or 'fix' in task_name.lower():
            issue_type = 'Bug'
        elif 'epic' in task_name.lower():
            issue_type = 'Epic'
        elif 'story' in task_name.lower():
            issue_type = 'Story'
        
        # Create issue
        issue = self.jira.create_issue(title, description, issue_type=issue_type)
        
        if issue:
            # For API v2, we need to fetch the full issue details to get the created date
            # API v2 create response: {'id': '59659', 'key': 'ALTP-4', 'self': '...'}
            # API v3 create response includes 'fields' with metadata
            created_date = None
            if 'fields' in issue and 'created' in issue['fields']:
                # API v3 response includes created date
                created_date = issue['fields']['created']
            else:
                # API v2 response - fetch full issue details
                full_issue = self.jira.get_issue(issue['key'])
                if full_issue and 'fields' in full_issue and 'created' in full_issue['fields']:
                    created_date = full_issue['fields']['created']
                else:
                    # Fallback to current time if we can't get created date
                    created_date = datetime.now().isoformat()
            
            # Save mapping
            def _mut(mappings):
                mappings[task_name] = {
                    'jira_issue_id': issue['id'],
                    'jira_issue_key': issue['key'],
                    'jira_url': f"{self.jira.base_url}/browse/{issue['key']}",
                    'task_file': f'team-management/tasks/{task_name}.md',
                    'sync_direction': 'bidirectional',
                    'created': created_date,
                    'last_synced': datetime.now().isoformat(),
                    'status': 'pending'
                }
                return True
            self._locked_mapping_update(_mut)
            return issue['key']

        return None

    # Note: _collect_agent_summaries() and _generate_rich_completion_comment()
    # are now inherited from IssueTrackingTaskSync base class.

    def sync_task_status_to_issue(self, task_name: str, status: str,
                                 additional_info: Dict = None):
        """Sync task status to the linked Jira issue"""
        mapping = self.get_task_mapping(task_name)
        if not mapping:
            return

        # Mapping may exist without an issue key. Fall back to the numeric id:
        # Jira's REST API accepts BOTH a key (PROJ-123) and a numeric id, so a
        # task linked by numeric id (jira_issue_key stored as None) still syncs
        # instead of silently early-returning here. MR/PR-only mappings (neither
        # key nor id) still fall through to the return.
        issue_key = mapping.get('jira_issue_key') or mapping.get('jira_issue_id')
        if not issue_key:
            return

        # Map Claude task statuses to Jira statuses
        status_map = {
            'completed': 'Done',
            'blocked': 'Blocked',
            'in-progress': 'In Progress',
            'pending': 'To Do'
        }

        jira_status = status_map.get(status, status)

        # Add status update comment - use rich comment for completed tasks
        if status == 'completed':
            # Use rich completion comment for completed tasks
            comment = self._generate_rich_completion_comment(task_name, additional_info)
        else:
            # Use basic comment for other statuses
            comment = f"**Task Status Update from Claude Code**: {status}"
            if additional_info:
                comment += f"\n\nAdditional Info: {json.dumps(additional_info, indent=2)}"

        self.jira.add_comment(issue_key, comment)

        # Try to update issue status if possible
        try:
            self.jira.update_issue(issue_key, status=jira_status)
        except Exception as e:
            _log(f"Could not update Jira issue status: {e}")

        # Update last synced time
        def _mut(mappings):
            if task_name in mappings:
                mappings[task_name]['last_synced'] = datetime.now().isoformat()
                return True
            return False
        self._locked_mapping_update(_mut)


def get_jira_sync() -> Optional[JiraTaskSync]:
    """Get Jira task sync instance, returns None if not configured"""
    try:
        return JiraTaskSync()
    except Exception as e:
        _log(f"Jira sync initialization failed: {e}")
        return None