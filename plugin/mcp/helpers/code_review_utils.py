"""
Code review helper utilities.

Provides URL parsing, branch validation, git environment management,
and context gathering for code review tools.
"""

import re
import subprocess
from typing import Dict, Any, List, Optional

from core.project import get_project_root, setup_provider_imports


def validate_branch_name(branch_name: str) -> bool:
    """
    Validate branch name contains only safe characters.

    Thin delegator to the shared validator in hooks/git_operations.py (single
    source of truth, also used by the engine/optimize git funcs). Lazily
    imported so a standalone import of this helper (before the server puts the
    hooks dir on sys.path) falls back to the identical inline rule.

    Args:
        branch_name: Branch name to validate

    Returns:
        True if valid, False otherwise
    """
    try:
        from git_operations import validate_branch_name as _shared
    except ImportError:  # pragma: no cover - defensive; server sets sys.path first
        # A leading dash would be parsed as a git option (option injection).
        if not branch_name or branch_name.startswith('-'):
            return False
        return bool(re.match(r'^[a-zA-Z0-9/._-]+$', branch_name))
    return _shared(branch_name)


def parse_mr_pr_url(url: str) -> Optional[Dict[str, Any]]:
    """
    Parse MR/PR URL to extract provider type, ID, and other metadata.

    Supports:
    - GitLab MR URLs: https://gitlab.com/namespace/project/-/merge_requests/123
    - GitHub PR URLs: https://github.com/owner/repo/pull/456

    Returns:
        Dict with 'provider', 'id', 'base_url', 'project_path'/'repository', or None if invalid
    """

    url = url.strip()

    # Prevent ReDoS: limit URL depth to prevent catastrophic backtracking
    if url.count('/') > 20:
        return None

    # Try GitLab MR pattern (limited to 10 path segments to prevent ReDoS)
    gitlab_pattern = r'^(https?://[^/]+)/((?:[^/]+/){1,10}[^/]+)/-/merge_requests/(\d+)(?:[/#?].*)?$'
    gitlab_match = re.match(gitlab_pattern, url)

    if gitlab_match:
        base_url, project_path, mr_iid = gitlab_match.groups()
        return {
            'provider': 'gitlab',
            'id': mr_iid,
            'base_url': base_url,
            'project_path': project_path,
            'url': url
        }

    # Try GitHub PR patterns. Anchored to the URL start so a crafted path like
    # https://evil.com/forward/github.com/owner/repo/pull/123 cannot mis-parse
    # the host. Scheme-less URLs intentionally do not parse -- provider
    # web_url fields always carry a scheme.
    github_patterns = [
        r'^https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)',  # Public GitHub
        r'^https?://([^/.]+\.[^/]+)/([^/]+)/([^/]+)/pull/(\d+)',  # GitHub Enterprise
    ]

    for pattern in github_patterns:
        github_match = re.match(pattern, url)
        if github_match:
            groups = github_match.groups()
            pr_number = groups[-1]

            if len(groups) == 3:
                # Public GitHub: owner/repo/pull/123
                owner, repo, _ = groups
                repository = f"{owner}/{repo}"
                base_url = "https://api.github.com"
            else:
                # GitHub Enterprise: domain/owner/repo/pull/123
                domain, owner, repo, _ = groups
                repository = f"{owner}/{repo}"
                base_url = f"https://{domain}/api/v3"

            return {
                'provider': 'github',
                'id': pr_number,
                'base_url': base_url,
                'repository': repository,
                'url': url
            }

    return None


def extract_code_review_from_notes(notes: List[Dict]) -> Optional[str]:
    """
    Extract raw code review markdown text from MR notes/comments.

    Args:
        notes: List of note objects from GitLab API

    Returns:
        Full markdown text of the code review, or None if not found
    """
    # Find note with code review pattern (most recent first)
    for note in reversed(notes):
        body = note.get('body', '')

        # Check if this note contains a code review
        if re.search(r'# Code Review:', body):
            return body

    return None


def fetch_mr_pr_by_url(url_info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Fetch MR/PR metadata from provider API using parsed URL info.

    Args:
        url_info: Result from parse_mr_pr_url()

    Returns:
        MR/PR object with branch info, title, description, etc.
    """
    # Ensure provider imports are set up
    setup_provider_imports()

    provider = url_info['provider']
    mr_pr_id = url_info['id']

    try:
        if provider == 'gitlab':
            from gitlab_utils import GitLabAPI, GitLabMRManager
            gitlab = GitLabAPI()
            mr = GitLabMRManager(gitlab).get_merge_request(mr_pr_id)
            return mr

        elif provider == 'github':
            from github_utils import GitHubAPI
            github = GitHubAPI()
            endpoint = f"/repos/{url_info['repository']}/pulls/{mr_pr_id}"
            pr = github._make_request('GET', endpoint)
            return pr

    except Exception as e:
        raise Exception(f"Failed to fetch {provider.upper()} MR/PR #{mr_pr_id}: {e}")

    return None


def prepare_review_environment(branch_name: str) -> Dict[str, Any]:
    """
    Prepare git environment for code review by switching to target branch.

    Args:
        branch_name: Branch to switch to

    Returns:
        Dict with 'success', 'original_branch', 'stashed', 'errors'
    """
    project_root = get_project_root()

    result = {
        'success': False,
        'original_branch': None,
        'stashed': False,
        'switched': False,
        'errors': []
    }

    # Validate branch name to prevent command injection
    if not validate_branch_name(branch_name):
        result['errors'].append(f"Invalid branch name: '{branch_name}' contains unsafe characters")
        return result

    try:
        # 1. Save current branch
        branch_result = subprocess.run(
            ['git', 'branch', '--show-current'],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            cwd=project_root,
            check=True,
            timeout=15
        )
        result['original_branch'] = branch_result.stdout.strip()

        # Skip if already on target branch
        if result['original_branch'] == branch_name:
            result['success'] = True
            result['switched'] = False
            return result

        # 2. Check for uncommitted changes
        status_result = subprocess.run(
            ['git', 'status', '--porcelain'],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            cwd=project_root,
            check=True,
            timeout=15
        )

        has_changes = bool(status_result.stdout.strip())

        # 3. Stash uncommitted changes if any
        if has_changes:
            subprocess.run(
                ['git', 'stash', 'push', '-u', '-m', 'Temporary stash for code review'],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                cwd=project_root,
                check=True,
                timeout=30
            )
            result['stashed'] = True

        # 4. Fetch latest from remote
        subprocess.run(
            ['git', 'fetch', 'origin', branch_name],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            cwd=project_root,
            check=True,
            timeout=30
        )

        # 5. Checkout target branch
        subprocess.run(
            ['git', 'checkout', branch_name],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            cwd=project_root,
            check=True,
            timeout=10
        )

        # 6. Pull latest changes
        subprocess.run(
            ['git', 'pull', 'origin', branch_name],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            cwd=project_root,
            check=True,
            timeout=30
        )

        result['success'] = True
        result['switched'] = True

    except subprocess.TimeoutExpired as e:
        # Network ops (fetch/pull) or a stuck git hook can hang; surface a clear
        # timeout instead of the generic "Unexpected error" below.
        result['errors'].append(
            f"Git operation timed out (remote unreachable or a git hook hung): {e}"
        )
    except subprocess.CalledProcessError as e:
        error_msg = f"Git operation failed: {e.stderr if e.stderr else str(e)}"
        result['errors'].append(error_msg)
    except Exception as e:
        result['errors'].append(f"Unexpected error: {str(e)}")

    return result


def restore_git_environment(original_branch: str, was_stashed: bool) -> Dict[str, Any]:
    """
    Restore git environment after code review.

    Args:
        original_branch: Branch to restore
        was_stashed: Whether changes were stashed

    Returns:
        Dict with 'success', 'restored', 'unstashed', 'errors'
    """
    project_root = get_project_root()

    result = {
        'success': False,
        'restored': False,
        'unstashed': False,
        'errors': []
    }

    # 1. Switch back to original branch
    if original_branch:
        try:
            subprocess.run(
                ['git', 'checkout', original_branch],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                cwd=project_root,
                check=True,
                timeout=10
            )
            result['restored'] = True
        except subprocess.CalledProcessError as e:
            error_msg = f"Failed to checkout '{original_branch}': {e.stderr if e.stderr else str(e)}"
            result['errors'].append(error_msg)
            return result
        except Exception as e:
            error_msg = f"Unexpected checkout error: {str(e)}"
            result['errors'].append(error_msg)
            return result

    # 2. Pop stashed changes if any
    if was_stashed:
        try:
            # Check if stash exists before popping
            stash_list = subprocess.run(
                ['git', 'stash', 'list'],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                cwd=project_root,
                check=True,
                timeout=10
            )

            # Pop the exact stash ref, not index 0: another process may have
            # pushed a stash between prepare and restore, in which case a bare
            # `git stash pop` would pop the interloper. The list line carries
            # the ref: "stash@{N}: On <branch>: Temporary stash for code review"
            stash_ref = None
            for line in stash_list.stdout.splitlines():
                if 'Temporary stash for code review' in line:
                    stash_ref = line.split(':', 1)[0].strip()
                    break  # first match = most recent

            if stash_ref:
                subprocess.run(
                    ['git', 'stash', 'pop', stash_ref],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    cwd=project_root,
                    check=True,
                    timeout=30
                )
                result['unstashed'] = True
            else:
                error_msg = "Stash 'Temporary stash for code review' not found in stash list"
                result['errors'].append(error_msg)

        except subprocess.TimeoutExpired as e:
            error_msg = (
                f"Git stash operation timed out: {e}. "
                "Stash needs manual resolution -- run `git stash list` and "
                "`git stash pop <stash@{N}>` by hand."
            )
            result['errors'].append(error_msg)
        except subprocess.CalledProcessError as e:
            error_msg = f"Failed to pop stash: {e.stderr if e.stderr else str(e)}"
            result['errors'].append(error_msg)
        except Exception as e:
            error_msg = f"Unexpected stash pop error: {str(e)}"
            result['errors'].append(error_msg)

    # Success requires the branch restored AND, if changes were stashed,
    # the stash actually popped back -- a silently dropped stash is data loss.
    result['success'] = result['restored'] and (not was_stashed or result['unstashed'])

    return result
