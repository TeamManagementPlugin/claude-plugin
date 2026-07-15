"""
Project root detection and task file utilities.

Provides functions for finding the project root, setting up provider imports,
and locating task files.
"""

import importlib
import sys
from pathlib import Path
from typing import Optional

# Cached project root
_project_root: Optional[Path] = None


def get_project_root() -> Path:
    """Find project root by looking for marker files/directories.

    Checks CLAUDE_PROJECT_DIR environment variable first, then searches
    for common project markers (.git, team-management, pyproject.toml).

    Deliberate divergence from ``shared_state.get_project_root``: this module is
    the bootstrap that puts the hooks dir on ``sys.path`` so ``shared_state`` can
    be imported at all (see ``setup_provider_imports`` / ``get_hooks_path``), so
    it CANNOT delegate to ``shared_state`` at import time — that would be a
    chicken-and-egg. Both resolvers are env-first on ``CLAUDE_PROJECT_DIR`` (the
    path that matters in the plugin runtime, and the one asserted equal in
    ``test/test_mcp_core.py``); only the cwd-walk fallback differs (shared_state
    walks up for a ``.claude`` dir; this walks up for any of the markers below).
    The legacy pip-era ``'sessions'`` marker was removed — that directory no
    longer exists post-plugin-conversion.

    Returns:
        Path to project root directory
    """
    global _project_root

    if _project_root is not None:
        return _project_root

    import os

    # Check CLAUDE_PROJECT_DIR environment variable first (set by Claude Code)
    if 'CLAUDE_PROJECT_DIR' in os.environ:
        project_dir = Path(os.environ['CLAUDE_PROJECT_DIR'])
        if project_dir.exists():
            _project_root = project_dir
            return _project_root

    # Fall back to searching from current directory
    current = Path.cwd()

    # Look for common project markers
    for parent in [current] + list(current.parents):
        if any((parent / marker).exists() for marker in ['.git', 'team-management', 'pyproject.toml']):
            _project_root = parent
            return _project_root

    _project_root = current
    return _project_root


def get_hooks_path() -> Path:
    """Get the path to provider utility hooks.

    Returns the path where provider utilities (gitlab_utils.py, etc.) are located.
    Checks installed location first (.claude/hooks), then development location.

    Returns:
        Path to hooks directory

    Raises:
        Exception: If provider utilities cannot be found
    """
    import os
    project_root = get_project_root()

    # Resolve the provider-utility hooks directory across the three-root model.
    # Order: plugin install (read-only) -> deployed copy in project -> dev source.
    candidates = []
    # 1. Plugin install: CLAUDE_PLUGIN_ROOT set by Claude Code for plugin processes.
    plugin_root_env = os.environ.get('CLAUDE_PLUGIN_ROOT')
    if plugin_root_env:
        candidates.append(Path(plugin_root_env) / 'hooks')
    # 2. Deployed copy in the project (installer layout / legacy single-source-of-truth).
    candidates.append(project_root / '.claude' / 'hooks')
    # 3. Dev / source checkout: this file is plugin/mcp/core/project.py -> plugin/hooks.
    candidates.append(Path(__file__).resolve().parent.parent.parent / 'hooks')
    # 4. Legacy dev layout relative to project root.
    candidates.append(project_root / 'plugin' / 'hooks')

    for cand in candidates:
        if cand.exists() and (cand / 'gitlab_utils.py').exists():
            return cand

    raise Exception(
        f"Invalid plugin installation at {project_root}. "
        "Provider utilities not found in the plugin root, .claude/hooks, or plugin/hooks."
    )


def setup_provider_imports() -> None:
    """Add hooks directory to sys.path for provider utility imports.

    This must be called before importing provider utilities like
    gitlab_utils, jira_utils, or github_utils.
    """
    hooks_path = get_hooks_path()
    if str(hooks_path) not in sys.path:
        sys.path.insert(0, str(hooks_path))


def _import_from_hooks(module_name: str):
    """Ensure the hooks dir is on sys.path, then import and return the named module.

    Single helper that replaces the scattered
    `setup_provider_imports(); from <module> import <names>` idiom — call sites
    do `mod = _import_from_hooks("shared_state")` and read `mod.PROJECT_ROOT`, etc.

    Args:
        module_name: A hooks module name, e.g. "shared_state", "protocol_engine",
            "notification_utils".

    Returns:
        The imported module object.
    """
    setup_provider_imports()
    return importlib.import_module(module_name)


def find_task_file(task_name: str) -> Optional[Path]:
    """Find a task file by name, searching in tasks/ and subdirectories.

    Wrapper around issue_provider_base.find_task_file() that uses the global project_root.
    See issue_provider_base.find_task_file for full documentation.

    Args:
        task_name: Name of the task (without path or .md extension)

    Returns:
        Path to task file if found, None otherwise
    """
    # Ensure provider imports are set up
    setup_provider_imports()

    # Import shared implementation from issue_provider_base
    from issue_provider_base import find_task_file as _find_task_file
    return _find_task_file(get_project_root(), task_name)


def get_task_relative_path(task_file: Path) -> str:
    """Get the relative path of a task file from team-management/tasks/.

    Args:
        task_file: Absolute path to task file

    Returns:
        Relative path like "m-task.md" or "feature/subtask.md"
    """
    tasks_dir = get_project_root() / 'team-management' / 'tasks'
    try:
        return str(task_file.relative_to(tasks_dir))
    except ValueError:
        return task_file.name
