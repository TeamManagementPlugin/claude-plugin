"""
Issue tracking MCP tools.

Provides tools for issue status, CRUD operations, linking, syncing,
and provider API access across GitLab, Jira, and GitHub/Gitea.
"""

from typing import Dict, Any, List, Optional

from core.project import setup_provider_imports, find_task_file, get_task_relative_path
from core.config import load_config, detect_provider, get_provider_sync, get_provider_api


def register_tools(mcp):
    """Register issue tracking tools with the FastMCP server."""

    @mcp.tool()
    def issue_status() -> Dict[str, Any]:
        """
        Show active issue tracking provider status and configuration.

        Returns information about the configured provider, linked tasks,
        and connection details.
        """
        try:
            provider = detect_provider()

            if not provider:
                return {
                    "success": False,
                    "error": "No issue tracking provider enabled",
                    "setup_instructions": [
                        "Edit team-management/config.json",
                        "Set issue_tracking.provider to 'gitlab', 'jira', or 'github'",
                        "Enable and configure the chosen provider"
                    ]
                }

            config = load_config()
            sync = get_provider_sync()
            api = get_provider_api()
            mappings = sync._load_mappings()

            result = {
                "success": True,
                "provider": provider.upper(),
                "linked_tasks": 0,  # set to len(result["tasks"]) after the provider loop
                "tasks": []
            }

            # Provider-specific details
            if provider == 'gitlab':
                result["base_url"] = api.base_url
                result["project_path"] = api.project_path
                result["default_labels"] = api.default_labels

                for task, mapping in mappings.items():
                    # Surface the project iid as the round-trip handle: the gitlab:<n>
                    # id must feed back into issue_set_status/comment/read, all of which
                    # use the iid REST path. The global id 404s there. MR-only mappings
                    # lack gitlab_issue_iid too, so the skip still drops them.
                    issue_iid = mapping.get('gitlab_issue_iid')
                    if not issue_iid:
                        continue
                    result["tasks"].append({
                        "task": task,
                        "issue_id": f"gitlab:{issue_iid}",
                        "issue_number": issue_iid,
                        "last_synced": mapping.get('last_synced', 'never')
                    })
            elif provider == 'jira':
                result["base_url"] = api.base_url
                result["project_key"] = api.project_key
                result["default_issue_type"] = api.default_issue_type
                result["supported_types"] = api.supported_types

                for task, mapping in mappings.items():
                    issue_key = mapping.get('jira_issue_key', mapping.get('jira_issue_id'))
                    result["tasks"].append({
                        "task": task,
                        "issue_id": f"jira:{issue_key}",
                        "last_synced": mapping.get('last_synced', 'never')
                    })
            elif provider == 'github':
                result["base_url"] = api.base_url
                result["repository"] = api.repository
                result["default_labels"] = api.default_labels
                result["workflow_labels"] = api.workflow_labels

                for task, mapping in mappings.items():
                    # PR-only mappings (no linked issue) have no issue_id;
                    # skip them so `tasks` stays "issue-linked only" — the contract
                    # the task protocol's issue-linking validation relies on.
                    issue_id = mapping.get('issue_id')
                    if not issue_id:
                        continue
                    result["tasks"].append({
                        "task": task,
                        "issue_id": f"github:{issue_id}",
                        "issue_number": issue_id,
                        "last_synced": mapping.get('last_synced', 'never')
                    })

            # Count only issue-linked tasks actually listed, so linked_tasks stays
            # consistent with `tasks` when MR/PR-only mappings are skipped above.
            result["linked_tasks"] = len(result["tasks"])
            return result

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "provider": detect_provider()
            }

    @mcp.tool()
    def config_issue_tracking_status() -> Dict[str, Any]:
        """
        Get issue tracking configuration status for protocol decision-making.

        Returns provider enabled status, which provider is active, and whether
        the configuration is complete. Used by task-creation and other protocols
        to determine if issue tracking integration is required.

        Returns:
            Dict with enabled, provider, provider_config, auto_sync,
            configuration_complete, and missing_fields
        """
        try:
            config = load_config()
            provider = detect_provider()

            result = {
                "success": True,
                "enabled": provider is not None,
                "provider": provider,
                "provider_config": {},
                "auto_sync": False,
                "configuration_complete": False,
                "missing_fields": []
            }

            if not provider:
                result["missing_fields"] = ["issue_tracking.provider"]
                return result

            provider_cfg = config.get(provider, {})
            missing = []

            # Token resolution is file-first (.claude/state/provider-tokens.json,
            # keyed by provider name) → config.json fallback, matching the provider
            # API wrappers. So a token set only in the per-project token file is seen
            # here, not reported "missing" while the provider calls succeed.
            setup_provider_imports()
            from shared_state import resolve_provider_token
            provider_token = resolve_provider_token(provider, provider_cfg.get("api_token"))

            if provider == "gitlab":
                result["provider_config"] = {
                    "base_url": provider_cfg.get("base_url", "https://gitlab.com"),
                    "project_path": provider_cfg.get("project_path", ""),
                    "has_token": bool(provider_token),
                    "default_labels": provider_cfg.get("default_labels", [])
                }
                if not provider_token:
                    missing.append(".claude/state/provider-tokens.json (key: gitlab)")
                if not provider_cfg.get("project_path"):
                    missing.append("gitlab.project_path")
                result["auto_sync"] = provider_cfg.get("auto_sync", False)

            elif provider == "jira":
                result["provider_config"] = {
                    "base_url": provider_cfg.get("base_url", ""),
                    "project_key": provider_cfg.get("project_key", ""),
                    "has_token": bool(provider_token),
                    "default_issue_type": provider_cfg.get("default_issue_type", "Task")
                }
                if not provider_token:
                    missing.append(".claude/state/provider-tokens.json (key: jira)")
                if not provider_cfg.get("base_url"):
                    missing.append("jira.base_url")
                if not provider_cfg.get("project_key"):
                    missing.append("jira.project_key")
                result["auto_sync"] = provider_cfg.get("auto_sync", False)

            elif provider == "github":
                result["provider_config"] = {
                    "base_url": provider_cfg.get("base_url", "https://api.github.com"),
                    "repository": provider_cfg.get("repository", ""),
                    "has_token": bool(provider_token),
                    "default_labels": provider_cfg.get("default_labels", [])
                }
                if not provider_token:
                    missing.append(".claude/state/provider-tokens.json (key: github)")
                if not provider_cfg.get("repository"):
                    missing.append("github.repository")
                result["auto_sync"] = provider_cfg.get("auto_sync", False)

            result["missing_fields"] = missing
            result["configuration_complete"] = len(missing) == 0

            return result

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "enabled": False,
                "provider": None,
                "configuration_complete": False,
                "missing_fields": []
            }

    @mcp.tool()
    def config_code_review_enforcement(task_content: str = None) -> Dict[str, Any]:
        """
        Get code review enforcement configuration and optionally analyze warnings.

        Args:
            task_content: Optional task file content to analyze for warnings

        Returns:
            Dict with enforce_warnings, enforcement_mode, analysis, and guidance
        """
        try:
            # Ensure provider imports are set up
            setup_provider_imports()

            from protocol_utils import (
                get_code_review_enforcement_config,
                check_code_review_warnings,
                get_review_completion_language
            )

            # Get configuration
            config = get_code_review_enforcement_config()
            enforce_warnings = config.get("code_review", {}).get("enforce_warnings", False)

            # Get guidance language
            guidance = get_review_completion_language(enforce_warnings)

            result = {
                "success": True,
                "enforce_warnings": enforce_warnings,
                "enforcement_mode": "strict" if enforce_warnings else "relaxed",
                "analysis": None,
                "guidance": guidance
            }

            # Optionally analyze task content for warnings
            if task_content:
                analysis = check_code_review_warnings(task_content)
                result["analysis"] = analysis

            return result

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "enforce_warnings": False,
                "enforcement_mode": "relaxed",
                "analysis": None,
                "guidance": {}
            }

    @mcp.tool()
    def issue_read(issue_id_or_url: str, update_mode: bool = False) -> Dict[str, Any]:
        """
        Import an issue as a Claude task.

        Args:
            issue_id_or_url: Issue ID or full URL
            update_mode: If True, update existing task file instead of failing

        Returns:
            Created task information including task name, file path, and link status
        """
        try:
            # Ensure provider imports are set up
            setup_provider_imports()

            provider = detect_provider()
            sync = get_provider_sync()

            task_name = sync.import_issue_as_task(issue_id_or_url, update_mode=update_mode)

            # Format display ID with provider prefix
            raw_id = str(issue_id_or_url)
            display_id = issue_id_or_url

            if provider == 'gitlab':
                if isinstance(issue_id_or_url, str) and issue_id_or_url.startswith('http'):
                    from gitlab_utils import parse_gitlab_issue_url
                    url_parts = parse_gitlab_issue_url(issue_id_or_url)
                    if url_parts:
                        raw_id = url_parts['issue_id']
                elif isinstance(issue_id_or_url, str) and issue_id_or_url.startswith('gitlab:'):
                    raw_id = issue_id_or_url[7:]
                display_id = f"gitlab:{raw_id}"

            elif provider == 'jira':
                if isinstance(issue_id_or_url, str) and issue_id_or_url.startswith('http'):
                    url_parts = sync.jira.parse_issue_url(issue_id_or_url)
                    if url_parts:
                        raw_id = url_parts['issue_key']
                elif isinstance(issue_id_or_url, str) and issue_id_or_url.startswith('jira:'):
                    raw_id = issue_id_or_url[5:]
                display_id = f"jira:{raw_id}"

            elif provider == 'github':
                if isinstance(issue_id_or_url, str) and issue_id_or_url.startswith('http'):
                    url_parts = sync.github.parse_issue_url(issue_id_or_url)
                    if url_parts:
                        raw_id = url_parts['issue_id']
                elif isinstance(issue_id_or_url, str) and issue_id_or_url.startswith('github:'):
                    raw_id = issue_id_or_url[7:]
                display_id = f"github:{raw_id}"

            # Find the created task file to report accurate path
            created_task_file = find_task_file(task_name)
            task_file_path = get_task_relative_path(created_task_file) if created_task_file else f"{task_name}.md"

            return {
                "success": True,
                "provider": provider.upper(),
                "task_name": task_name,
                "task_file": f"team-management/tasks/{task_file_path}",
                "issue_id": display_id,
                "next_steps": [
                    f"Start the task protocol to adopt this task and create its branch: "
                    f"protocol_start(protocol_name='task', task='{task_name}')",
                    "The protocol engine sets task state and creates the branch whose prefix "
                    "matches the task name — do not edit .claude/state/current_task.json or "
                    "hardcode a 'feature/' branch."
                ]
            }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "provider": detect_provider()
            }

    @mcp.tool()
    def issue_create(task_name: str) -> Dict[str, Any]:
        """
        Create an issue from a Claude task.

        Args:
            task_name: Name of the task (without path or .md extension)

        Returns:
            Created issue information including issue ID and link status
        """
        try:
            provider = detect_provider()
            sync = get_provider_sync()

            if provider == 'gitlab':
                issue_id = sync.create_gitlab_issue_from_task(task_name)
                display_id = f"gitlab:{issue_id}"
            elif provider == 'jira':
                issue_key = sync.create_issue_from_task(task_name)
                issue_id = issue_key
                display_id = f"jira:{issue_key}"
            elif provider == 'github':
                issue_number = sync.create_issue_from_task(task_name)
                issue_id = issue_number
                display_id = f"github:{issue_number}"
            else:
                raise Exception("Unknown provider")

            if issue_id:
                return {
                    "success": True,
                    "provider": provider.upper(),
                    "task_name": task_name,
                    "issue_id": display_id,
                    "linked": True
                }
            else:
                return {
                    "success": False,
                    "error": "Failed to create issue",
                    "provider": provider.upper()
                }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "provider": detect_provider()
            }

    @mcp.tool()
    def issue_update(issue_id: str, title: str = None, description: str = None,
                    status: str = None, labels: List[str] = None) -> Dict[str, Any]:
        """
        Update an existing issue.

        Args:
            issue_id: Issue ID (can be prefixed like "github:123" or raw "123")
            title: New title (optional)
            description: New description/body (optional)
            status: New status (optional) - provider-specific values
            labels: New labels list (optional)

        Returns:
            Update status and issue information
        """
        try:
            provider = detect_provider()
            api = get_provider_api()

            # Extract raw issue ID from prefixed format if needed
            raw_issue_id = issue_id
            if ':' in issue_id:
                raw_issue_id = issue_id.split(':', 1)[1]

            # Update issue using provider API
            updated_issue = api.update_issue(
                raw_issue_id,
                title=title,
                description=description,
                status=status,
                labels=labels
            )

            if updated_issue:
                return {
                    "success": True,
                    "provider": provider.upper(),
                    "issue_id": f"{provider}:{raw_issue_id}",
                    "updated": True,
                    "updated_fields": {
                        "title": title is not None,
                        "description": description is not None,
                        "status": status is not None,
                        "labels": labels is not None
                    }
                }
            else:
                return {
                    "success": False,
                    "error": "No fields to update",
                    "provider": provider.upper()
                }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "provider": detect_provider()
            }

    @mcp.tool()
    def issue_sync(task_name: str) -> Dict[str, Any]:
        """
        Sync task status to linked issue.

        Args:
            task_name: Name of the task to sync

        Returns:
            Sync status and updated issue state
        """
        try:
            provider = detect_provider()
            sync = get_provider_sync()

            # Find task file (supports subdirectories)
            task_file = find_task_file(task_name)
            if not task_file:
                return {
                    "success": False,
                    "error": f"Task file not found: {task_name}.md (searched in team-management/tasks/ and subdirectories)"
                }

            # Parse status from frontmatter
            with open(task_file, 'r', encoding='utf-8') as f:
                content = f.read()

            status = 'pending'
            lines = content.split('\n')
            in_frontmatter = False
            for line in lines:
                if line.strip() == '---':
                    if in_frontmatter:
                        break
                    in_frontmatter = True
                elif in_frontmatter and line.startswith('status:'):
                    status = line.split(':', 1)[1].strip()
                    break

            # Perform sync
            if provider == 'gitlab':
                sync.sync_task_status_to_gitlab(task_name, status)
            elif provider == 'jira':
                sync.sync_task_status_to_issue(task_name, status)
            elif provider == 'github':
                sync.sync_task_status_to_issue(task_name, status)

            return {
                "success": True,
                "provider": provider.upper(),
                "task_name": task_name,
                "status": status,
                "synced": True
            }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "provider": detect_provider()
            }

    @mcp.tool()
    def issue_push(task_name: str) -> Dict[str, Any]:
        """
        Push task content to linked issue (update issue description from task file).

        Args:
            task_name: Name of the task to push

        Returns:
            Push status including whether title and description were updated
        """
        try:
            provider = detect_provider()
            sync = get_provider_sync()

            # Find task file (supports subdirectories)
            task_file = find_task_file(task_name)
            if not task_file:
                return {
                    "success": False,
                    "error": f"Task file not found: {task_name}.md (searched in team-management/tasks/ and subdirectories)"
                }

            # Perform update
            if provider == 'gitlab':
                success = sync.update_issue_description_from_task(task_name)
            elif provider == 'jira':
                return {
                    "success": False,
                    "error": "issue_push not yet implemented for Jira. Use issue_sync instead.",
                    "provider": provider.upper()
                }
            elif provider == 'github':
                success = sync.update_issue_description_from_task(task_name)
            else:
                raise Exception(f"Unknown provider: {provider}")

            if success:
                return {
                    "success": True,
                    "provider": provider.upper(),
                    "task_name": task_name,
                    "description_updated": True,
                    "message": "Issue description updated from task file"
                }
            else:
                return {
                    "success": False,
                    "error": "Failed to update issue description",
                    "provider": provider.upper()
                }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "provider": detect_provider()
            }

    @mcp.tool()
    def issue_link(task_name: str, issue_id: str) -> Dict[str, Any]:
        """
        Link an existing task to an issue.

        Args:
            task_name: Name of the task
            issue_id: Issue ID (numeric for GitLab, key for Jira)

        Returns:
            Link status
        """
        try:
            provider = detect_provider()
            sync = get_provider_sync()

            if provider == 'gitlab':
                try:
                    issue_id_int = int(issue_id)
                    sync.link_task_to_issue(task_name, issue_id_int)
                    display_id = f"gitlab:{issue_id}"
                except ValueError:
                    return {
                        "success": False,
                        "error": f"Invalid GitLab issue ID: {issue_id}. Must be numeric.",
                        "provider": provider.upper()
                    }
            elif provider == 'jira':
                success = sync.link_task_to_issue(task_name, issue_id)
                if not success:
                    return {
                        "success": False,
                        "error": f"Failed to link task '{task_name}' to Jira issue '{issue_id}'.",
                        "provider": provider.upper()
                    }
                display_id = f"jira:{issue_id}"
            elif provider == 'github':
                try:
                    issue_id_int = int(issue_id)
                    success = sync.link_task_to_issue(task_name, issue_id_int)
                    if not success:
                        return {
                            "success": False,
                            "error": f"Failed to link task '{task_name}' to GitHub issue #{issue_id}.",
                            "provider": provider.upper()
                        }
                    display_id = f"github:{issue_id}"
                except ValueError:
                    return {
                        "success": False,
                        "error": f"Invalid GitHub issue ID: {issue_id}. Must be numeric.",
                        "provider": provider.upper()
                    }
            else:
                raise Exception("Unknown provider")

            return {
                "success": True,
                "provider": provider.upper(),
                "task_name": task_name,
                "issue_id": display_id,
                "linked": True
            }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "provider": detect_provider()
            }

    @mcp.tool()
    def issue_unlink(task_name: str) -> Dict[str, Any]:
        """
        Remove issue link from a task.

        Args:
            task_name: Name of the task to unlink

        Returns:
            Unlink status
        """
        try:
            provider = detect_provider()
            sync = get_provider_sync()

            sync.unlink_task(task_name)

            return {
                "success": True,
                "provider": provider.upper(),
                "task_name": task_name,
                "unlinked": True
            }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "provider": detect_provider()
            }

    @mcp.tool()
    def issue_comment(issue_id: str, comment: str) -> Dict[str, Any]:
        """
        Add a comment to an issue.

        Args:
            issue_id: Issue ID (e.g., "123" for GitHub/GitLab, "PROJ-123" for Jira)
            comment: Comment text (markdown supported)

        Returns:
            Comment creation status with comment ID
        """
        try:
            provider = detect_provider()
            api = get_provider_api()

            # Input validation
            if not comment or not comment.strip():
                return {
                    "success": False,
                    "error": "Comment cannot be empty"
                }

            # Normalize issue_id - strip provider prefix if present
            raw_issue_id = issue_id
            if ':' in issue_id:
                raw_issue_id = issue_id.split(':', 1)[1]

            result = api.add_comment(raw_issue_id, comment)

            # Format display ID with provider prefix
            display_id = f"{provider}:{raw_issue_id}"

            return {
                "success": True,
                "provider": provider.upper(),
                "issue_id": display_id,
                "comment_id": result.get('id') if result else None,
                "comment_url": result.get('html_url') or result.get('self') if result else None
            }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "provider": detect_provider()
            }

    @mcp.tool()
    def issue_set_status(issue_id: str, status: str, comment: str = None) -> Dict[str, Any]:
        """
        Change the status of an issue.

        Args:
            issue_id: Issue ID (e.g., "123" for GitHub/GitLab, "PROJ-123" for Jira)
            status: Target status. Provider-specific values:
                - GitHub/Gitea: "open", "closed"
                - GitLab: "reopen", "close"
                - Jira: Transition name (e.g., "In Progress", "Done")
            comment: Optional status change comment

        Returns:
            Status change result
        """
        try:
            provider = detect_provider()
            api = get_provider_api()

            # Input validation
            if not status or not status.strip():
                return {
                    "success": False,
                    "error": "Status cannot be empty"
                }

            status = status.strip()

            # Normalize issue_id
            raw_issue_id = issue_id
            if ':' in issue_id:
                raw_issue_id = issue_id.split(':', 1)[1]

            # Add comment if provided
            if comment and comment.strip():
                api.add_comment(raw_issue_id, comment)

            # Update status based on provider
            if provider == 'gitlab':
                status_lower = status.lower()
                if status_lower in ('closed', 'close'):
                    api.update_issue(raw_issue_id, state_event='close')
                    final_status = 'closed'
                elif status_lower in ('open', 'reopen', 'reopened'):
                    api.update_issue(raw_issue_id, state_event='reopen')
                    final_status = 'opened'
                else:
                    return {
                        "success": False,
                        "error": f"Invalid GitLab status: '{status}'. Use 'open'/'reopen' or 'closed'/'close'"
                    }

            elif provider == 'github':
                status_lower = status.lower()
                if status_lower in ('closed', 'close'):
                    api.update_issue(raw_issue_id, status='closed')
                    final_status = 'closed'
                elif status_lower in ('open', 'reopen', 'reopened'):
                    api.update_issue(raw_issue_id, status='open')
                    final_status = 'open'
                else:
                    return {
                        "success": False,
                        "error": f"Invalid GitHub/Gitea status: '{status}'. Use 'open' or 'closed'"
                    }

            elif provider == 'jira':
                api.update_issue(raw_issue_id, status=status)
                final_status = status

            else:
                raise Exception(f"Unknown provider: {provider}")

            # Format display ID with provider prefix
            display_id = f"{provider}:{raw_issue_id}"

            return {
                "success": True,
                "provider": provider.upper(),
                "issue_id": display_id,
                "status": final_status
            }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "provider": detect_provider()
            }

    @mcp.tool()
    def issue_api(method: str, endpoint: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Make a custom API call to the issue tracking provider.

        ADVANCED: Power-user tool for operations not covered by other tools.

        Args:
            method: HTTP method (GET, POST, PUT, PATCH, DELETE)
            endpoint: API endpoint path relative to provider base URL
            data: Optional JSON data for POST/PUT/PATCH requests

        Returns:
            Raw API response or error details
        """
        try:
            provider = detect_provider()
            api = get_provider_api()

            # Input validation
            method = method.upper()
            if method not in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE'):
                return {
                    "success": False,
                    "error": f"Invalid HTTP method: {method}. Allowed: GET, POST, PUT, PATCH, DELETE"
                }

            if not endpoint or not endpoint.startswith('/'):
                return {
                    "success": False,
                    "error": "Endpoint must start with '/'"
                }

            # Sanitize endpoint - prevent path traversal
            if '..' in endpoint:
                return {
                    "success": False,
                    "error": "Invalid endpoint: path traversal not allowed"
                }

            # Make the API request
            result = api._make_request(method, endpoint, data)

            # Check for error response
            if isinstance(result, dict) and result.get('error'):
                return {
                    "success": False,
                    "provider": provider.upper(),
                    "error_type": result.get('error_type', 'unknown'),
                    "status_code": result.get('status_code'),
                    "message": result.get('message', 'API request failed'),
                    "response_body": result.get('response_body'),
                    "url": result.get('url'),
                    "method": method
                }

            return {
                "success": True,
                "provider": provider.upper(),
                "method": method,
                "endpoint": endpoint,
                "response": result
            }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "provider": detect_provider()
            }

    @mcp.tool()
    def issue_dependency(
        issue_number: int,
        depends_on_issue: int,
        remove: bool = False,
        depends_on_repo: Optional[str] = None,
        depends_on_owner: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Create or remove a dependency between two issues.

        Supports Gitea (full) and GitHub (same-repo only). Not supported on GitLab.

        Args:
            issue_number: The issue that will be blocked
            depends_on_issue: The issue that blocks
            remove: If True, remove the dependency
            depends_on_repo: Repository for cross-repo deps (Gitea only)
            depends_on_owner: Owner for cross-repo deps (Gitea only)

        Returns:
            Success status and dependency details
        """
        try:
            provider = detect_provider()
            config = load_config()

            if provider == 'gitlab':
                return {
                    "success": False,
                    "error": "GitLab issue dependencies not supported",
                    "hint": "GitLab uses 'issue links' API with link_type='blocks'"
                }

            if provider != 'github':
                return {
                    "success": False,
                    "error": f"Unknown provider: {provider}"
                }

            api = get_provider_api()
            github_config = config.get('github', {})
            repository = github_config.get('repository', '')

            if '/' not in repository:
                return {
                    "success": False,
                    "error": "Invalid repository format. Expected 'owner/repo'"
                }

            current_owner, current_repo = repository.split('/', 1)

            # Default to current repo/owner for the dependency target
            target_owner = depends_on_owner or current_owner
            target_repo = depends_on_repo or current_repo

            # Detect if this is Gitea or GitHub
            base_url = github_config.get('base_url', 'https://api.github.com')
            is_gitea = '/api/v1' in base_url or 'gitea' in base_url.lower()

            if is_gitea:
                provider_name = "GITEA"
            else:
                # GitHub doesn't support cross-repo dependencies
                if target_owner != current_owner or target_repo != current_repo:
                    return {
                        "success": False,
                        "error": "GitHub does not support cross-repository dependencies",
                        "hint": "Remove depends_on_owner and depends_on_repo parameters"
                    }
                provider_name = "GITHUB"

            if remove:
                if is_gitea:
                    endpoint = f"/repos/{current_owner}/{current_repo}/issues/{issue_number}/dependencies"
                    result = api._make_request('DELETE', endpoint, {
                        "owner": target_owner,
                        "repo": target_repo,
                        "index": depends_on_issue
                    })
                else:
                    endpoint = f"/repos/{current_owner}/{current_repo}/issues/{issue_number}/dependencies/blocked_by/{depends_on_issue}"
                    result = api._make_request('DELETE', endpoint, None)
                action = "removed"
            else:
                if is_gitea:
                    endpoint = f"/repos/{current_owner}/{current_repo}/issues/{issue_number}/dependencies"
                    result = api._make_request('POST', endpoint, {
                        "owner": target_owner,
                        "repo": target_repo,
                        "index": depends_on_issue
                    })
                else:
                    endpoint = f"/repos/{current_owner}/{current_repo}/issues/{issue_number}/dependencies/blocked_by"
                    result = api._make_request('POST', endpoint, {
                        "issue_id": depends_on_issue
                    })
                action = "created"

            # Check for error response
            if isinstance(result, dict) and result.get('error'):
                return {
                    "success": False,
                    "provider": provider_name,
                    "error_type": result.get('error_type', 'unknown'),
                    "status_code": result.get('status_code'),
                    "message": result.get('message', f'Failed to {action.rstrip("d")} dependency'),
                    "response_body": result.get('response_body'),
                    "url": result.get('url')
                }

            return {
                "success": True,
                "provider": provider_name,
                "action": action,
                "issue_number": issue_number,
                "depends_on": {
                    "owner": target_owner,
                    "repo": target_repo,
                    "issue": depends_on_issue
                },
                "message": f"Issue #{issue_number} dependency on {target_owner}/{target_repo}#{depends_on_issue} {action}"
            }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "provider": detect_provider()
            }
