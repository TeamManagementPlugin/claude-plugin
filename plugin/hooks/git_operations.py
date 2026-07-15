#!/usr/bin/env python3
"""
Shared git operations for issue tracking providers.

Provides common git utilities used by GitLab, GitHub, and other providers
for reading commit history between branches.
"""
import os
import re
import subprocess
from pathlib import Path
from typing import List, Optional

from engine_constants import GIT_TIMEOUT_FAST


_BRANCH_NAME_RE = re.compile(r'^[a-zA-Z0-9/._-]+$')


def validate_branch_name(branch: str) -> bool:
    """Return True iff ``branch`` is a safe git branch name for subprocess argv.

    Rejects empty names, a leading dash (git would parse it as an option —
    option-injection), and any character outside ``[A-Za-z0-9/._-]``. This is
    the single source of truth shared by the MCP git tools
    (``mcp/tools/git_operations.py``, ``mcp/helpers/code_review_utils.py``) and
    the engine/optimize git funcs (``protocol_engine.py``,
    ``optimize_completion.py``) — deliberately stricter than git's full ref
    grammar (no Unicode, no ``@``, ``~``, ``^``, ``:``) because the framework's
    own branch names (``fix/…`` / ``feature/…`` / ``optimize/…`` /
    ``brainstorm/…``) all fall inside this charset.
    """
    if not isinstance(branch, str) or not branch or branch.startswith('-'):
        return False
    return bool(_BRANCH_NAME_RE.match(branch))


def run_git(args: List[str], description: str, cwd: Path) -> Optional[subprocess.CompletedProcess]:
    """Run a git command with error handling.

    Args:
        args: Git command arguments (e.g., ['git', 'status'])
        description: Human-readable description for error messages
        cwd: Working directory for command execution

    Returns:
        CompletedProcess object on success, None on error
    """
    # Configurable timeout via environment variable (default: 30 seconds)
    timeout_seconds = int(os.getenv("BCC_GIT_TIMEOUT_SECONDS", "30"))

    try:
        result = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout_seconds
        )
        return result
    except subprocess.TimeoutExpired:
        print(f"Git operation timed out: {description}")
        return None
    except Exception as e:
        print(f"Git operation failed: {description}: {e}")
        return None


def get_commit_log(source_branch: str, target_branch: str, cwd: Path) -> List[str]:
    """Get commit messages between two branches.

    Args:
        source_branch: Source branch
        target_branch: Target branch
        cwd: Working directory (project root)

    Returns:
        List of commit subject lines
    """
    result = run_git(
        ['git', 'log', f'{target_branch}..{source_branch}', '--pretty=format:%s', '--reverse'],
        'Get commit log',
        cwd
    )

    if not result or result.returncode != 0:
        return []

    commits = result.stdout.strip().split('\n')
    # Filter out merge commits and empty lines
    return [c for c in commits if c and not c.startswith('Merge ')]


def detect_default_branch(project_root) -> str:
    """Return the repository's default branch name (best-effort, never raises).

    Preference order (hard-coding main/master breaks repos with custom default
    branches like ``develop`` / ``trunk``):
      1. ``git symbolic-ref refs/remotes/origin/HEAD`` — authoritative on any
         repo cloned from a remote (records the remote's default, regardless of
         local branch layout).
      2. First existing local branch among the common candidates
         (``main`` / ``master`` / ``develop`` / ``trunk`` / ``stable``).
      3. ``"main"`` as a last-resort fallback.

    Returns a bare branch name (no ``origin/`` prefix). Single source of truth
    for ``OptimizeCompletionMixin._detect_default_branch`` and the MCP
    ``git_operations`` tool.
    """
    cwd = str(project_root)

    try:
        symref = subprocess.run(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=GIT_TIMEOUT_FAST, check=False, cwd=cwd,
        )
        if symref.returncode == 0:
            # Output is like `origin/main`; strip the remote prefix.
            ref = symref.stdout.strip()
            if ref.startswith("origin/"):
                ref = ref[len("origin/"):]
            if ref:
                return ref
    except (subprocess.SubprocessError, OSError):
        pass

    for candidate in ("main", "master", "develop", "trunk", "stable"):
        try:
            check = subprocess.run(
                ["git", "rev-parse", "--verify", candidate],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=GIT_TIMEOUT_FAST, check=False, cwd=cwd,
            )
            if check.returncode == 0:
                return candidate
        except (subprocess.SubprocessError, OSError):
            continue
    return "main"


def ensure_tag_created_and_pushed(tag_name, release_name, project_root, log=None):
    """Create the annotated git tag locally if missing, then push it to origin.

    Single source of truth shared by the GitLab and GitHub/Gitea release managers
    (extracted from their near-identical inline blocks). Behaviour-preserving:
      - Creates the tag only if it does not already exist locally
        (``git tag -a <tag> -m "Release <name>"``), then ``git push origin <tag>``.
      - ``release_name`` is stripped and its CR/LF collapsed to spaces before use
        in the tag message (message-injection guard, preserved verbatim).
      - ``log``: optional ``callable(str)`` for progress messages. GitLab passes
        its provider logger (4 progress lines); GitHub passes ``None`` (silent) —
        matching each provider's prior behaviour exactly.

    Raises:
        Exception: on a ``git`` ``CalledProcessError`` (message includes the
            decoded stderr) or on a push ``TimeoutExpired``.
    """
    tag = tag_name.strip()
    try:
        # Check if the tag already exists locally.
        result = subprocess.run(
            ['git', 'tag', '-l', tag],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            cwd=project_root, check=True,
        )
        tag_exists_locally = bool(result.stdout.strip())

        if not tag_exists_locally:
            if log:
                log(f"Tag '{tag_name}' not found locally, creating...")
            # SECURITY: sanitize release_name for the git tag message.
            safe_release_name = release_name.strip().replace('\n', ' ').replace('\r', ' ')
            subprocess.run(
                ['git', 'tag', '-a', tag, '-m', f'Release {safe_release_name}'],
                stdin=subprocess.DEVNULL, cwd=project_root, check=True, capture_output=True,
            )
            if log:
                log(f"Created tag '{tag_name}' at current commit")

        if log:
            log(f"Pushing tag '{tag_name}' to remote...")
        subprocess.run(
            ['git', 'push', 'origin', tag],
            stdin=subprocess.DEVNULL, cwd=project_root, check=True,
            capture_output=True, timeout=30,
        )
        if log:
            log(f"Successfully pushed tag '{tag_name}' to remote")

    except subprocess.CalledProcessError as e:
        try:
            error_output = e.stderr.decode('utf-8', errors='replace') if e.stderr else str(e)
        except Exception:
            error_output = str(e)
        raise Exception(f"Failed to create/push tag '{tag_name}': {error_output}")
    except subprocess.TimeoutExpired:
        raise Exception(f"Timeout while pushing tag '{tag_name}' to remote")
