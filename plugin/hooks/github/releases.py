#!/usr/bin/env python3
"""
GitHub/Gitea release management.

Handles release creation, asset uploads, and tag management.
"""
import re
try:
    import requests
except ImportError:  # cold plugin session before the venv exists
    requests = None
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .api import GitHubAPI


from shared_state import get_provider_logger
from git_operations import ensure_tag_created_and_pushed

_log = get_provider_logger('github-releases.log')


class GitHubReleaseManager:
    """Manages GitHub/Gitea release operations."""

    def __init__(self, api: 'GitHubAPI'):
        """Initialize with GitHub API instance."""
        self.api = api
        self.project_root = api.project_root
        self.repository = api.repository
        self.api_token = api.api_token

    def _upload_release_asset(self, release_id: int, upload_url: str, file_path: str) -> Optional[Dict]:
        """Upload asset to GitHub release."""
        if requests is None:  # cold session without venv
            return None
        file = Path(file_path).resolve()
        if not file.exists():
            raise ValueError(f"Asset file not found: {file_path}")

        if not file.is_file():
            raise ValueError(f"Asset path is not a file: {file_path}")

        # SECURITY: Prevent path traversal
        project_root_resolved = Path(self.project_root).resolve()
        try:
            file.relative_to(project_root_resolved)
        except ValueError:
            raise ValueError(f"Asset path outside project directory: {file_path}")

        # Check file size (GitHub has 2GB per asset limit)
        size_mb = file.stat().st_size / (1024 * 1024)
        if size_mb > 2048:
            raise ValueError(f"Asset too large: {size_mb:.1f}MB (max: 2048MB)")

        try:
            parsed_url = upload_url.replace('{?name,label}', f'?name={urllib.parse.quote(file.name)}')

            with open(file, 'rb') as f:
                file_data = f.read()

            upload_headers = {
                'Authorization': f'token {self.api_token}',
                'Content-Type': 'application/octet-stream'
            }

            response = requests.post(parsed_url, headers=upload_headers, data=file_data, timeout=120)
            response.raise_for_status()

            return response.json()

        except Exception as e:
            # Redact the token — this raw requests.post bypasses the base
            # _make_request HTTPError path that normally scrubs it.
            _log(self.api._redact_token(f"Warning: Failed to upload release asset {file_path}: {e}"))
            return None

    def get_release(self, tag_name: str) -> Optional[Dict]:
        """Get release by tag name."""
        if not tag_name or not isinstance(tag_name, str):
            raise ValueError("Tag name must be a non-empty string")

        if len(tag_name) > 128:
            raise ValueError("Tag name too long (max 128 characters)")

        result = self.api._make_request('GET', f"/repos/{self.repository}/releases/tags/{tag_name}")

        if result and result.get('error') and result.get('status_code') == 404:
            return None
        return self.api._raise_on_error(result, "GitHub API error getting release")

    def create_release(self, tag_name: str, release_name: str, description: str,
                      asset_paths: List[str] = None, draft: bool = False,
                      prerelease: bool = False) -> Optional[Dict]:
        """Create a GitHub release with optional file uploads."""
        if not tag_name or len(tag_name.strip()) == 0:
            raise ValueError("Tag name cannot be empty")
        if len(tag_name) > 128:
            raise ValueError("Tag name too long (max 128 characters)")

        if not release_name or len(release_name.strip()) == 0:
            raise ValueError("Release name cannot be empty")
        if len(release_name) > 255:
            raise ValueError("Release name too long (max 255 characters)")

        if description and len(description) > 100000:
            raise ValueError("Description too long (max 100000 characters)")

        # SECURITY: Tag name validation
        TAG_SAFE_PATTERN = r'^[a-zA-Z0-9._-]+$'
        if not re.match(TAG_SAFE_PATTERN, tag_name.strip()):
            raise ValueError(f"Tag name contains invalid characters: {tag_name}")

        # Check if release already exists
        existing = self.get_release(tag_name)
        if existing:
            raise Exception(f"Release already exists for tag '{tag_name}'")

        # Create the tag locally if missing, then push it to origin.
        ensure_tag_created_and_pushed(tag_name, release_name, self.project_root)

        # Create release
        data = {
            'tag_name': tag_name.strip(),
            'name': release_name.strip(),
            'body': description or '',
            'draft': draft,
            'prerelease': prerelease
        }

        result = self.api._make_request('POST', f"/repos/{self.repository}/releases", data)

        self.api._raise_on_error(result, "GitHub API error creating release")

        # Upload assets if provided
        if result and asset_paths:
            if len(asset_paths) > 10:
                raise ValueError("Too many assets (max 10 per release)")

            release_id = result.get('id')
            upload_url = result.get('upload_url')

            if release_id and upload_url:
                for asset_path in asset_paths:
                    self._upload_release_asset(release_id, upload_url, asset_path)

        return result
