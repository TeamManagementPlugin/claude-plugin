"""
Configuration management for MCP server.

Provides configuration loading with caching, provider detection,
and provider API/sync class instantiation.
"""

import json
from typing import Optional, Dict, Any

from core.project import get_project_root, setup_provider_imports

# Configuration and provider state
_config: Optional[Dict[str, Any]] = None
_provider: Optional[str] = None
_config_mtime: Optional[float] = None


def reload_config() -> None:
    """Force reload configuration from disk (invalidates cache).

    Also clears provider singleton instances to ensure they pick up
    new configuration on next access.
    """
    global _config, _provider, _config_mtime
    _config = None
    _provider = None
    _config_mtime = None

    # Clear provider singleton instances if they exist
    # This ensures get_provider_api/sync return fresh instances with new config
    try:
        from core.project import setup_provider_imports
        setup_provider_imports()
        try:
            from github_utils import _clear_singletons
            _clear_singletons()
        except (ImportError, AttributeError):
            pass  # Function may not exist in older versions
    except Exception:
        pass  # Don't fail reload if imports fail


def load_config() -> Dict[str, Any]:
    """Load configuration from config.json with automatic cache invalidation.

    Uses mtime-based cache invalidation to detect config file changes.

    Returns:
        Configuration dictionary (empty dict if file doesn't exist)
    """
    global _config, _config_mtime

    project_root = get_project_root()
    config_file = project_root / 'team-management' / 'config.json'

    # One stat() (no exists()+stat() race): a missing/unreadable file yields the
    # -1.0 sentinel, distinguishable from any real mtime (>= 0). Tracking it in
    # EVERY cache state below — including the missing-file and exception paths —
    # lets the invalidation fire on an existence change in BOTH directions: a
    # config.json that appears after a first (file-absent) load (the fresh-install
    # flow: MCP server starts, THEN /team-management:init + :config), or one that
    # is later deleted. The old guard only checked mtime while the file existed and
    # left _config_mtime = None on the empty-cache paths, so once {} was cached
    # before config.json existed it was returned for the whole server lifetime.
    try:
        current_mtime = config_file.stat().st_mtime
        file_exists = True
    except OSError:
        current_mtime = -1.0
        file_exists = False

    if _config is not None and _config_mtime is not None and current_mtime != _config_mtime:
        # File existence or mtime changed since last load — invalidate cache.
        reload_config()

    if _config is not None:
        return _config

    if not file_exists:
        _config = {}
        _config_mtime = -1.0
        return _config

    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            _config = json.load(f)
        _config_mtime = current_mtime
        return _config
    except Exception:
        # Silently fail - don't break STDIO protocol. Cache the file's mtime (not
        # the None sentinel) so a stably-malformed file is not re-read every call;
        # a later fix (new mtime) trips the invalidation above and is picked up.
        _config = {}
        _config_mtime = current_mtime
        return _config


def detect_provider() -> Optional[str]:
    """Detect active issue tracking provider from configuration.

    Checks issue_tracking.provider setting first, then falls back to
    checking individual provider sections.

    Returns:
        Provider name ('gitlab', 'jira', 'github') or None if disabled
    """
    global _provider

    # Run load_config() FIRST: its mtime-based invalidation calls
    # reload_config(), which clears a stale cached _provider. Checking the
    # cache before loading made a provider change invisible until restart.
    config = load_config()

    if _provider is not None:
        return _provider

    # Check unified issue_tracking.provider setting
    issue_tracking = config.get('issue_tracking', {})
    provider = issue_tracking.get('provider', 'disabled')

    if provider != 'disabled':
        # Validate that provider is enabled
        provider_config = config.get(provider, {})
        if provider_config.get('enabled'):
            _provider = provider
            return _provider

    # Fallback: check individual provider settings
    if config.get('gitlab', {}).get('enabled'):
        _provider = 'gitlab'
        return _provider
    elif config.get('jira', {}).get('enabled'):
        _provider = 'jira'
        return _provider
    elif config.get('github', {}).get('enabled'):
        _provider = 'github'
        return _provider

    _provider = None
    return None


def get_provider_sync():
    """Get the appropriate sync class for the active provider.

    Uses singleton pattern for GitHub/Gitea to maintain label cache efficiency.
    Gitea requires label IDs (not names), so caching prevents redundant API calls.

    Returns:
        Provider-specific sync class instance

    Raises:
        Exception: If no provider is enabled
    """
    # Ensure provider imports are set up
    setup_provider_imports()

    provider = detect_provider()

    if provider == 'gitlab':
        from gitlab_utils import GitLabTaskSync
        return GitLabTaskSync()
    elif provider == 'jira':
        from jira_utils import JiraTaskSync
        return JiraTaskSync()
    elif provider == 'github':
        # Use singleton to maintain label cache and avoid duplicate label creation
        from github_utils import get_github_sync
        sync = get_github_sync()
        if not sync:
            raise Exception("Failed to initialize GitHub/Gitea sync")
        return sync
    else:
        raise Exception(
            "No issue tracking provider enabled. "
            "Configure GitLab, Jira, or GitHub in config.json"
        )


def get_provider_api():
    """Get the appropriate API class for the active provider.

    Uses singleton instances to maintain consistent state (e.g., label cache)
    across multiple MCP tool calls.

    Returns:
        Provider-specific API class instance

    Raises:
        Exception: If no provider is enabled
    """
    # Ensure provider imports are set up
    setup_provider_imports()

    provider = detect_provider()

    if provider == 'gitlab':
        from gitlab_utils import GitLabAPI
        return GitLabAPI()
    elif provider == 'jira':
        from jira_utils import JiraProvider
        return JiraProvider()
    elif provider == 'github':
        # Use singleton to maintain label cache and avoid duplicate label creation
        from github_utils import get_github_api
        api = get_github_api()
        if not api:
            raise Exception("Failed to initialize GitHub/Gitea API")
        return api
    else:
        raise Exception(
            "No issue tracking provider enabled. "
            "Configure GitLab, Jira, or GitHub in config.json"
        )
