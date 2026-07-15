"""
Git and merge request MCP tools.

Provides tools for git commit, push, and GitLab merge request operations.
"""

import re
import subprocess
from typing import Dict, Any, List

from core.project import get_project_root, setup_provider_imports
from core.config import load_config

# Reuse the shared git-subprocess timeout convention. engine_constants lives in
# the hooks dir, put on sys.path by setup_provider_imports() at server start —
# before this module is imported. Fall back to the same literal magnitudes if the
# hooks dir is not yet on sys.path (exotic standalone import), mirroring the
# module's other optional hooks-dir dependencies.
try:
    from engine_constants import GIT_TIMEOUT_FAST, GIT_TIMEOUT_MEDIUM, GIT_TIMEOUT_SLOW
except ImportError:  # pragma: no cover - defensive; the server always sets up the path
    GIT_TIMEOUT_FAST, GIT_TIMEOUT_MEDIUM, GIT_TIMEOUT_SLOW = 15, 30, 60

# Shared branch-name validator (single source of truth in hooks/git_operations.py).
# Same import-with-fallback shape as the timeouts above: the hooks dir is on
# sys.path at server start, before this module imports; the fallback covers an
# exotic standalone import.
try:
    from git_operations import validate_branch_name
except ImportError:  # pragma: no cover - defensive; the server always sets up the path
    def validate_branch_name(_b):
        return bool(_b) and not _b.startswith('-') and bool(re.match(r'^[a-zA-Z0-9/._-]+$', _b))


def _detect_default_branch(project_root) -> str:
    """Best-effort default-branch detection for MR/PR targets.

    Thin delegator to the shared hooks-level ``detect_default_branch``
    (git_operations.py) — the single source of truth also used by
    ``OptimizeCompletionMixin._detect_default_branch``. Imported lazily because
    the hooks dir is added to ``sys.path`` at server startup
    (``setup_provider_imports``), which may run after this module is imported.
    Kept as a module-level name so tests can
    ``patch.object(git_operations, "_detect_default_branch", ...)``.
    """
    from git_operations import detect_default_branch
    return detect_default_branch(project_root)


def register_tools(mcp):
    """Register git operation tools with the FastMCP server."""

    @mcp.tool()
    def git_commit(message: str, add_all: bool = True) -> Dict[str, Any]:
        """
        Commit changes to git with a message.

        IMPORTANT: Only call this tool when the user explicitly asks to commit.
        Do NOT call autonomously as part of a workflow or protocol.

        Args:
            message: Commit message. Multi-line messages are supported (e.g. a
                body plus `Co-Authored-By:` trailers) — safe here because the
                commit runs via subprocess.run(shell=False) with list args, so
                no shell metacharacter is ever interpreted.
            add_all: Whether to stage all changes first (default: True)

        Returns:
            Commit status
        """
        try:
            project_root = get_project_root()

            # Input validation
            if not message or len(message.strip()) == 0:
                return {
                    "success": False,
                    "error": "Commit message cannot be empty"
                }
            if len(message) > 1000:
                return {
                    "success": False,
                    "error": "Commit message too long (max 1000 characters)"
                }

            # The commit runs via subprocess.run(shell=False) with list args, so
            # no shell metacharacter is interpreted — backticks, `$`, and
            # newlines are all safe, and multi-line messages (Co-Authored-By
            # trailers) MUST be allowed. Only a NUL byte is rejected: git cannot
            # store it and it would truncate the argument.
            if '\x00' in message:
                return {
                    "success": False,
                    "error": "Commit message contains a NUL byte"
                }

            if add_all:
                subprocess.run(
                    ['git', 'add', '.'],
                    stdin=subprocess.DEVNULL,
                    cwd=project_root,
                    check=True,
                    capture_output=True,
                    timeout=GIT_TIMEOUT_MEDIUM
                )

            subprocess.run(
                ['git', 'commit', '-m', message.strip()],
                stdin=subprocess.DEVNULL,
                cwd=project_root,
                check=True,
                capture_output=True,
                timeout=GIT_TIMEOUT_MEDIUM
            )

            return {
                "success": True,
                "message": message,
                "committed": True
            }
        except subprocess.TimeoutExpired as e:
            return {
                "success": False,
                "error": f"Git commit timed out after {GIT_TIMEOUT_MEDIUM}s "
                         f"(a git hook or index lock may be stuck): {e}"
            }
        except subprocess.CalledProcessError as e:
            return {
                "success": False,
                "error": f"Git commit failed: {e}"
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }

    @mcp.tool()
    def git_push(branch: str = None) -> Dict[str, Any]:
        """
        Push current branch to remote.

        IMPORTANT: Only call this tool when the user explicitly asks to push.
        Do NOT call autonomously as part of a workflow or protocol.

        Args:
            branch: Branch name to push (defaults to current branch)

        Returns:
            Push status
        """
        try:
            project_root = get_project_root()

            # Get current branch if not specified
            if not branch:
                result = subprocess.run(
                    ['git', 'branch', '--show-current'],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    cwd=project_root,
                    check=True,
                    timeout=GIT_TIMEOUT_FAST
                )
                branch = result.stdout.strip()

            # Validate branch name format; a leading dash would be parsed as
            # a git option (option injection). Shared validator (hooks/git_operations.py).
            if not validate_branch_name(branch):
                return {
                    "success": False,
                    "error": "Invalid branch name format (only alphanumeric, /, ., _, - allowed; leading '-' rejected)"
                }

            # Push with upstream tracking
            subprocess.run(
                ['git', 'push', '-u', 'origin', branch],
                stdin=subprocess.DEVNULL,
                cwd=project_root,
                check=True,
                capture_output=True,
                timeout=GIT_TIMEOUT_SLOW
            )

            return {
                "success": True,
                "branch": branch,
                "pushed": True
            }
        except subprocess.TimeoutExpired as e:
            return {
                "success": False,
                "error": f"Git push timed out after {GIT_TIMEOUT_SLOW}s "
                         f"(remote unreachable or a credential helper is hung): {e}"
            }
        except subprocess.CalledProcessError as e:
            return {
                "success": False,
                "error": f"Git push failed: {e}"
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }

    @mcp.tool()
    def merge_request_create(task_name: str, source_branch: str,
                            target_branch: str = None,
                            labels: List[str] = None) -> Dict[str, Any]:
        """
        Create a merge request linked to task's GitLab issue.

        IMPORTANT: Only call this tool when the user explicitly asks to create a merge request.
        Do NOT call autonomously as part of a workflow or protocol.

        Args:
            task_name: Name of the task
            source_branch: Source branch name
            target_branch: Target branch name (default: the repo's detected
                default branch — origin/HEAD, else main/master/develop/…)
            labels: Labels to apply to MR (default: inherit from issue + config defaults)

        Returns:
            Created merge request information
        """
        try:
            # Validate task name for path traversal
            if '..' in task_name or task_name.startswith('/') or '\\' in task_name:
                return {
                    "success": False,
                    "error": "Invalid task name: path traversal detected"
                }

            # Default the MR target to the repo's real default branch instead
            # of a hard-coded "master" (wrong on main-/develop-default repos).
            if target_branch is None:
                target_branch = _detect_default_branch(get_project_root())

            # Check if GitLab is configured (independent of issue tracking provider)
            config = load_config()
            gitlab_config = config.get('gitlab', {})
            if not gitlab_config.get('enabled'):
                return {
                    "success": False,
                    "error": "GitLab is not configured. Enable GitLab in config.json to use merge requests."
                }

            # Ensure provider imports are set up
            setup_provider_imports()

            from gitlab_utils import GitLabTaskSync, GitLabMRManager
            sync = GitLabTaskSync()

            # Get task mapping to find issue IID. MR-only mappings are truthy but
            # lack gitlab_issue_iid; this tool creates an issue-linked MR, so treat
            # a missing issue IID as "not linked".
            mapping = sync.get_task_mapping(task_name)
            issue_iid = mapping.get('gitlab_issue_iid') if mapping else None
            if not issue_iid:
                return {
                    "success": False,
                    "error": f"Task {task_name} not linked to GitLab issue"
                }

            # Create merge request. The MR methods moved to GitLabMRManager after
            # the provider split; the API instance (sync.gitlab) no longer carries
            # them. (create_merge_request_from_issue never existed on GitLabAPI.)
            mr = GitLabMRManager(sync.gitlab).create_merge_request(
                issue_iid,
                source_branch,
                target_branch,
                task_name=task_name,
                labels=labels
            )

            if mr:
                return {
                    "success": True,
                    "provider": "GITLAB",
                    "task_name": task_name,
                    "merge_request_iid": mr['iid'],
                    "merge_request_url": mr['web_url'],
                    "source_branch": source_branch,
                    "target_branch": target_branch
                }
            else:
                return {
                    "success": False,
                    "error": "Failed to create merge request"
                }

        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }

    @mcp.tool()
    def merge_request_update(mr_iid: int, title: str = None,
                            description: str = None,
                            state_event: str = None) -> Dict[str, Any]:
        """
        Update an existing GitLab merge request.

        IMPORTANT: Only call this tool when the user explicitly asks to update a merge request.
        Do NOT call autonomously as part of a workflow or protocol.

        Args:
            mr_iid: Merge request IID (project-specific ID)
            title: New title (optional)
            description: New description (optional)
            state_event: State change - "close" or "reopen" (optional)

        Returns:
            Update status
        """
        try:
            # Input validation
            if mr_iid <= 0:
                return {
                    "success": False,
                    "error": "Invalid merge request IID: must be positive integer"
                }

            if title is not None and len(title) > 255:
                return {
                    "success": False,
                    "error": "Title too long (max 255 characters)"
                }

            if description is not None and len(description) > 10000:
                return {
                    "success": False,
                    "error": "Description too long (max 10000 characters)"
                }

            if state_event is not None and state_event not in ['close', 'reopen']:
                return {
                    "success": False,
                    "error": "Invalid state_event: must be 'close' or 'reopen'"
                }

            # Check if GitLab is configured (independent of issue tracking provider)
            config = load_config()
            gitlab_config = config.get('gitlab', {})
            if not gitlab_config.get('enabled'):
                return {
                    "success": False,
                    "error": "GitLab is not configured. Enable GitLab in config.json to use merge requests."
                }

            # Ensure provider imports are set up
            setup_provider_imports()

            from gitlab_utils import GitLabAPI, GitLabMRManager
            gitlab = GitLabAPI()

            mr = GitLabMRManager(gitlab).update_merge_request(mr_iid, title, description, state_event)

            if mr:
                return {
                    "success": True,
                    "provider": "GITLAB",
                    "merge_request_iid": mr['iid'],
                    "merge_request_url": mr['web_url'],
                    "updated": True
                }
            else:
                return {
                    "success": False,
                    "error": "Failed to update merge request"
                }

        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }
