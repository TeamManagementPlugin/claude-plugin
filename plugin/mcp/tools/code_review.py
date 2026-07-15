"""
Code review MCP tools.

Provides tools for MR/PR comments, automated code review,
and fetching existing reviews.
"""

import subprocess
import sys
from typing import Dict, Any

from core.project import get_project_root, setup_provider_imports, find_task_file
from core.config import load_config, detect_provider
from helpers.code_review_utils import (
    validate_branch_name,
    parse_mr_pr_url,
    extract_code_review_from_notes,
    fetch_mr_pr_by_url,
    prepare_review_environment,
    restore_git_environment,
)


def register_tools(mcp):
    """Register code review tools with the FastMCP server."""

    @mcp.tool()
    def merge_request_comment(mr_iid: int, comment: str) -> Dict[str, Any]:
        """
        Add a comment to a GitLab merge request.

        Args:
            mr_iid: Merge request IID (project-specific ID)
            comment: Comment text to add

        Returns:
            Comment status
        """
        try:
            # Check if GitLab is configured
            config = load_config()
            gitlab_config = config.get('gitlab', {})
            if not gitlab_config.get('enabled'):
                return {
                    "success": False,
                    "error": "GitLab is not configured. Enable GitLab in config.json."
                }

            # Input validation
            if mr_iid <= 0:
                return {
                    "success": False,
                    "error": "Invalid merge request IID: must be positive integer"
                }

            if not comment or not isinstance(comment, str):
                return {
                    "success": False,
                    "error": "Comment must be a non-empty string"
                }

            if len(comment) > 1000000:  # 1MB limit
                return {
                    "success": False,
                    "error": "Comment too long (max 1MB)"
                }

            setup_provider_imports()
            from gitlab_utils import GitLabAPI, GitLabMRManager
            gitlab = GitLabAPI()

            result = GitLabMRManager(gitlab).add_mr_comment(mr_iid, comment)

            if result:
                return {
                    "success": True,
                    "provider": "GITLAB",
                    "merge_request_iid": mr_iid,
                    "comment_added": True
                }
            else:
                return {
                    "success": False,
                    "error": "Failed to add comment"
                }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "provider": detect_provider()
            }

    @mcp.tool()
    def pull_request_comment(pr_number: int, comment: str) -> Dict[str, Any]:
        """
        Add a comment to a GitHub pull request.

        Args:
            pr_number: Pull request number
            comment: Comment text to add

        Returns:
            Comment status
        """
        try:
            # Check if GitHub is configured
            config = load_config()
            github_config = config.get('github', {})
            if not github_config.get('enabled'):
                return {
                    "success": False,
                    "error": "GitHub is not configured. Enable GitHub in config.json."
                }

            # Input validation
            if pr_number <= 0:
                return {
                    "success": False,
                    "error": "Invalid pull request number: must be positive integer"
                }

            if not comment or not isinstance(comment, str):
                return {
                    "success": False,
                    "error": "Comment must be a non-empty string"
                }

            if len(comment) > 1000000:  # 1MB limit
                return {
                    "success": False,
                    "error": "Comment too long (max 1MB)"
                }

            setup_provider_imports()
            from github_utils import GitHubAPI
            github = GitHubAPI()

            result = github.add_comment(pr_number, comment)

            if result:
                return {
                    "success": True,
                    "provider": "GITHUB",
                    "pull_request_number": pr_number,
                    "comment_added": True
                }
            else:
                return {
                    "success": False,
                    "error": "Failed to add comment"
                }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "provider": detect_provider()
            }

    @mcp.tool()
    def code_review(
        branch: str = None,
        task_name: str = None,
        mr_iid: int = None,
        pr_number: int = None,
        review_text: str = None,
        mr_pr_url: str = None
    ) -> Dict[str, Any]:
        """
        Run automated code review and post results to MR/PR if exists.

        Flexible invocation patterns:
        1. Task-based: code_review(task_name="m-task")
        2. MR-based: code_review(mr_iid=123)
        3. PR-based: code_review(pr_number=456)
        4. Branch-based: code_review(branch="feature/x")
        5. Auto: code_review()
        6. Post review: code_review(mr_iid=123, review_text="...")
        7. URL-based: code_review(mr_pr_url="https://...")

        Args:
            branch: Branch to review (auto-detected if not provided)
            task_name: Optional task name for context
            mr_iid: Optional GitLab MR IID
            pr_number: Optional GitHub PR number
            review_text: Optional pre-generated review text to post
            mr_pr_url: Optional MR/PR URL

        Returns:
            Dict with review results and MR/PR status
        """
        project_root = get_project_root()
        errors = []
        result = {
            "success": False,
            "review_completed": False,
            "review_results": None,
            "mr_pr_found": False,
            "mr_pr_number": None,
            "mr_pr_url": None,
            "comment_added": False,
            "provider": None,
            "branch": branch,
            "context_source": None,
            "errors": errors
        }

        # Track git environment for restoration
        git_env_switched = False
        git_env_info = None

        try:
            # Input validation for task_name if provided
            if task_name:
                if not isinstance(task_name, str):
                    return {"success": False, "error": "Task name must be a string"}
                if '..' in task_name or task_name.startswith('/') or '\\' in task_name:
                    return {"success": False, "error": "Invalid task name: path traversal detected"}

            # Ensure provider imports are set up
            setup_provider_imports()

            # Detect provider
            provider = detect_provider()
            result['provider'] = provider.upper() if provider else None

            # Phase 0: Handle MR/PR URL if provided
            if mr_pr_url:
                url_info = parse_mr_pr_url(mr_pr_url)
                if not url_info:
                    return {
                        "success": False,
                        "error": f"Invalid MR/PR URL format: {mr_pr_url}",
                        "supported_formats": [
                            "GitLab: https://gitlab.com/namespace/project/-/merge_requests/123",
                            "GitHub: https://github.com/owner/repo/pull/456"
                        ]
                    }

                url_provider = url_info['provider']

                try:
                    mr_or_pr = fetch_mr_pr_by_url(url_info)
                    if mr_or_pr:
                        result['mr_pr_found'] = True
                        result['mr_pr_number'] = url_info['id']
                        result['mr_pr_url'] = url_info['url']

                        if url_provider == 'gitlab':
                            branch = mr_or_pr.get('source_branch')
                            mr_iid = int(url_info['id'])
                        elif url_provider == 'github':
                            branch = mr_or_pr.get('head', {}).get('ref')
                            pr_number = int(url_info['id'])

                        # Validate branch name
                        if branch and not validate_branch_name(branch):
                            return {
                                "success": False,
                                "error": f"Invalid branch name from MR/PR: '{branch}' contains unsafe characters"
                            }

                        result['branch'] = branch

                        # Switch to branch for review if not already on it
                        if branch and not review_text:
                            git_env_info = prepare_review_environment(branch)
                            if git_env_info['success']:
                                git_env_switched = True
                            else:
                                errors.append(f"Git operations: {'; '.join(git_env_info['errors'])}")
                except Exception as e:
                    return {
                        "success": False,
                        "error": f"Failed to fetch MR/PR from URL: {e}",
                        "url": mr_pr_url
                    }

            # Phase 1: Determine branch
            if not branch:
                try:
                    branch_result = subprocess.run(
                        ['git', 'branch', '--show-current'],
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        text=True,
                        cwd=project_root,
                        check=True,
                        timeout=15  # GIT_TIMEOUT_FAST-style: local op, must not wedge the tool
                    )
                    branch = branch_result.stdout.strip()
                    result['branch'] = branch
                except Exception as e:
                    errors.append(f"Failed to determine current branch: {e}")

            # Phase 2: Fetch MR/PR if directly specified
            mr_or_pr = None
            if mr_iid and provider == 'gitlab' and not mr_pr_url:
                try:
                    from gitlab_utils import GitLabAPI, GitLabMRManager
                    gitlab = GitLabAPI()
                    mr_or_pr = GitLabMRManager(gitlab).get_merge_request(mr_iid)
                    if mr_or_pr:
                        result['mr_pr_found'] = True
                        result['mr_pr_number'] = mr_or_pr['iid']
                        result['mr_pr_url'] = mr_or_pr['web_url']
                        branch = mr_or_pr.get('source_branch', branch)
                        result['branch'] = branch

                        if branch and not review_text and not git_env_switched:
                            git_env_info = prepare_review_environment(branch)
                            if git_env_info['success']:
                                git_env_switched = True
                            else:
                                errors.append(f"Git operations: {'; '.join(git_env_info['errors'])}")
                except Exception as e:
                    errors.append(f"Failed to fetch GitLab MR #{mr_iid}: {e}")
                    return result

            elif pr_number and provider == 'github' and not mr_pr_url:
                try:
                    from github_utils import GitHubAPI
                    github = GitHubAPI()
                    endpoint = f"/repos/{github.repository}/pulls/{pr_number}"
                    pr = github._make_request('GET', endpoint)
                    if pr:
                        mr_or_pr = pr
                        result['mr_pr_found'] = True
                        result['mr_pr_number'] = pr['number']
                        result['mr_pr_url'] = pr['html_url']
                        branch = pr.get('head', {}).get('ref', branch)
                        result['branch'] = branch

                        if branch and not review_text and not git_env_switched:
                            git_env_info = prepare_review_environment(branch)
                            if git_env_info['success']:
                                git_env_switched = True
                            else:
                                errors.append(f"Git operations: {'; '.join(git_env_info['errors'])}")
                except Exception as e:
                    errors.append(f"Failed to fetch GitHub PR #{pr_number}: {e}")
                    return result

            # Phase 3: Gather context for code review
            context = ""
            context_source = "minimal"

            # Option A: Task-based context
            if task_name:
                task_file = find_task_file(task_name)
                if task_file:
                    try:
                        with open(task_file, 'r', encoding='utf-8') as f:
                            context = f.read()
                        context_source = "task"
                    except Exception as e:
                        errors.append(f"Failed to read task file: {e}")
                else:
                    errors.append(f"Task file not found: {task_name}.md")

            # Option B: MR/PR-based context
            if not context and mr_or_pr:
                context = f"""# Code Review Context from MR/PR

**Title**: {mr_or_pr.get('title', 'N/A')}
**Description**:
{mr_or_pr.get('description') or mr_or_pr.get('body', 'No description provided')}

**Branch**: {mr_or_pr.get('source_branch') or mr_or_pr.get('head', {}).get('ref', branch)}
**Target**: {mr_or_pr.get('target_branch') or mr_or_pr.get('base', {}).get('ref', 'N/A')}
"""
                context_source = "mr" if provider == 'gitlab' else "pr"

            # Option C: Auto-detect MR/PR by branch
            if not context and branch and not mr_or_pr:
                try:
                    if provider == 'gitlab':
                        from gitlab_utils import GitLabAPI, GitLabMRManager
                        gitlab = GitLabAPI()
                        mr_or_pr = GitLabMRManager(gitlab).find_merge_request_by_branch(branch, state="opened")
                        if mr_or_pr:
                            result['mr_pr_found'] = True
                            result['mr_pr_number'] = mr_or_pr['iid']
                            result['mr_pr_url'] = mr_or_pr['web_url']
                            context = f"""# Code Review Context from MR

**Title**: {mr_or_pr.get('title', 'N/A')}
**Description**:
{mr_or_pr.get('description', 'No description provided')}

**Branch**: {mr_or_pr.get('source_branch', branch)}
**Target**: {mr_or_pr.get('target_branch', 'N/A')}
"""
                            context_source = "mr"

                    elif provider == 'github':
                        from github_utils import GitHubAPI, GitHubPRManager
                        github = GitHubAPI()
                        pr = GitHubPRManager(github).find_pull_request_by_branch(branch, state="open")
                        if pr:
                            mr_or_pr = pr
                            result['mr_pr_found'] = True
                            result['mr_pr_number'] = pr['number']
                            result['mr_pr_url'] = pr['html_url']
                            context = f"""# Code Review Context from PR

**Title**: {pr.get('title', 'N/A')}
**Description**:
{pr.get('body', 'No description provided')}

**Branch**: {pr.get('head', {}).get('ref', branch)}
**Target**: {pr.get('base', {}).get('ref', 'N/A')}
"""
                            context_source = "pr"
                except Exception as e:
                    errors.append(f"Failed to auto-detect MR/PR: {e}")

            # Option D: Minimal context
            if not context:
                if branch:
                    context = f"# Code Review for Branch: {branch}\n\nNo task or MR/PR context available."
                    context_source = "branch"
                else:
                    errors.append("No context available: no task, MR/PR, or branch specified")
                    return result

            result['context_source'] = context_source

            # Phase 4: Handle review text
            if review_text:
                result['review_results'] = review_text
                result['review_completed'] = True

                # Try to find MR/PR if not already found
                if not result['mr_pr_found'] and branch:
                    try:
                        if provider == 'gitlab':
                            from gitlab_utils import GitLabAPI, GitLabMRManager
                            gitlab = GitLabAPI()
                            mr_or_pr = GitLabMRManager(gitlab).find_merge_request_by_branch(branch, state="opened")
                            if mr_or_pr:
                                result['mr_pr_found'] = True
                                result['mr_pr_number'] = mr_or_pr['iid']
                                result['mr_pr_url'] = mr_or_pr['web_url']
                        elif provider == 'github':
                            from github_utils import GitHubAPI, GitHubPRManager
                            github = GitHubAPI()
                            pr = GitHubPRManager(github).find_pull_request_by_branch(branch, state="open")
                            if pr:
                                mr_or_pr = pr
                                result['mr_pr_found'] = True
                                result['mr_pr_number'] = pr['number']
                                result['mr_pr_url'] = pr['html_url']
                    except Exception as e:
                        errors.append(f"Failed to find MR/PR for posting: {e}")

                # Post review to MR/PR if found
                if result['mr_pr_found']:
                    try:
                        review_comment = f"""## Code Review Results

{review_text}

---
Automated code review from [Claude Code](https://claude.com/claude-code)"""

                        if provider == 'gitlab':
                            from gitlab_utils import GitLabAPI, GitLabMRManager
                            gitlab = GitLabAPI()
                            GitLabMRManager(gitlab).add_mr_comment(result['mr_pr_number'], review_comment)
                            result['comment_added'] = True
                        elif provider == 'github':
                            from github_utils import GitHubAPI
                            github = GitHubAPI()
                            github.add_comment(result['mr_pr_number'], review_comment)
                            result['comment_added'] = True

                        result['success'] = True
                    except Exception as e:
                        errors.append(f"Failed to post review comment: {e}")
                        result['success'] = False
                else:
                    errors.append("No MR/PR found to post review to")
                    result['success'] = False

            else:
                # No review text - return context for manual agent invocation
                result['review_results'] = f"""## Code Review Requested

**Branch**: {branch or 'N/A'}
**Context Source**: {context_source}

**Context**:
{context[:500]}{'...' if len(context) > 500 else ''}

**Note**: To complete the review, run the code-review agent with this context.

**MR/PR Status**: {'Found' if result['mr_pr_found'] else 'Not found'}
{f"**MR/PR URL**: {result['mr_pr_url']}" if result['mr_pr_url'] else ''}

---
Two-Step Workflow for External Branches:
1. Run code-review agent in main conversation with the context above
2. Call code_review(mr_iid={mr_or_pr['iid'] if mr_or_pr and 'iid' in mr_or_pr else 'N'}, review_text="<agent output>") to post results
"""
                result['review_completed'] = True
                result['success'] = True

            # Try to find MR/PR by branch if not found yet
            if not review_text and not result['mr_pr_found'] and branch:
                try:
                    if provider == 'gitlab':
                        from gitlab_utils import GitLabAPI, GitLabMRManager
                        gitlab = GitLabAPI()
                        mr_or_pr = GitLabMRManager(gitlab).find_merge_request_by_branch(branch, state="opened")
                        if mr_or_pr:
                            result['mr_pr_found'] = True
                            result['mr_pr_number'] = mr_or_pr['iid']
                            result['mr_pr_url'] = mr_or_pr['web_url']

                    elif provider == 'github':
                        from github_utils import GitHubAPI, GitHubPRManager
                        github = GitHubAPI()
                        pr = GitHubPRManager(github).find_pull_request_by_branch(branch, state="open")
                        if pr:
                            mr_or_pr = pr
                            result['mr_pr_found'] = True
                            result['mr_pr_number'] = pr['number']
                            result['mr_pr_url'] = pr['html_url']
                except Exception as e:
                    errors.append(f"Failed to find MR/PR for posting: {e}")

            if not result['mr_pr_found']:
                errors.append(f"No open MR/PR found for branch '{branch}'.")

            return result

        except Exception as e:
            errors.append(f"Code review failed: {e}")
            return result

        finally:
            # Restore git environment if we switched branches
            if git_env_switched and git_env_info:
                try:
                    restore_result = restore_git_environment(
                        git_env_info.get('original_branch'),
                        git_env_info.get('stashed', False)
                    )
                    if not restore_result['success']:
                        restore_errors = '; '.join(restore_result['errors'])
                        result['errors'].append(f"Git restore: {restore_errors}")
                        print(f"WARNING: Git restore failed: {restore_errors}", file=sys.stderr)
                    else:
                        restored_branch = git_env_info.get('original_branch', 'unknown')
                        was_stashed = git_env_info.get('stashed', False)
                        print(f"INFO: Restored to '{restored_branch}'{' and popped stash' if was_stashed else ''}", file=sys.stderr)
                except Exception as e:
                    result['errors'].append(f"CRITICAL restore error: {str(e)}")
                    print(f"CRITICAL: Failed to restore git environment: {e}", file=sys.stderr)

    @mcp.tool()
    def fetch_mr_review(mr_url_or_iid: str) -> Dict[str, Any]:
        """
        Fetch code review from a GitLab merge request and return for LLM processing.

        Args:
            mr_url_or_iid: MR URL (https://...) or numeric IID

        Returns:
            Dict with MR metadata and review text
        """
        try:
            # Check if GitLab is configured
            config = load_config()
            gitlab_config = config.get('gitlab', {})
            if not gitlab_config.get('enabled'):
                return {
                    "success": False,
                    "error": "GitLab is not configured. Enable GitLab in config.json."
                }

            setup_provider_imports()
            from gitlab_utils import GitLabAPI, GitLabMRManager

            gitlab = GitLabAPI()
            mr_mgr = GitLabMRManager(gitlab)

            # Parse URL or handle IID
            if mr_url_or_iid.startswith('http'):
                parsed = parse_mr_pr_url(mr_url_or_iid)
                if not parsed or parsed['provider'] != 'gitlab':
                    return {
                        "success": False,
                        "error": f"Invalid GitLab MR URL: {mr_url_or_iid}",
                        "hint": "Expected: https://gitlab.com/namespace/project/-/merge_requests/123"
                    }
                mr_iid = parsed['id']
            else:
                try:
                    mr_iid = int(mr_url_or_iid)
                except ValueError:
                    return {
                        "success": False,
                        "error": f"Invalid MR IID: {mr_url_or_iid}. Must be numeric or valid URL."
                    }

            # Fetch MR metadata
            mr = mr_mgr.get_merge_request(mr_iid)
            if not mr:
                return {
                    "success": False,
                    "error": f"Merge request !{mr_iid} not found"
                }

            # Fetch MR notes/comments
            notes = mr_mgr.get_mr_notes(mr_iid)

            # Extract code review text
            review_text = extract_code_review_from_notes(notes)
            if not review_text:
                return {
                    "success": False,
                    "error": f"No code review found in MR !{mr_iid} comments",
                    "hint": "Run code review on the MR first, then try again",
                    "mr_url": mr.get('web_url')
                }

            return {
                "success": True,
                "provider": "GITLAB",
                "mr_title": mr['title'],
                "mr_iid": mr['iid'],
                "mr_url": mr['web_url'],
                "source_branch": mr.get('source_branch', 'N/A'),
                "target_branch": mr.get('target_branch', 'N/A'),
                "description": mr.get('description', ''),
                "review_text": review_text,
                "instruction": "Use this code review to create a task file with appropriate success criteria."
            }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "provider": detect_provider()
            }
