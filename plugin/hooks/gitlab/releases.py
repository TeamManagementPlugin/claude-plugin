#!/usr/bin/env python3
"""
GitLab release management.

Handles release creation, asset uploads, and tag management.
"""
import re
try:
    import requests
except ImportError:  # cold plugin session before the venv exists
    requests = None
from pathlib import Path
from typing import Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .api import GitLabAPI


from shared_state import get_provider_logger
from git_operations import ensure_tag_created_and_pushed

_log = get_provider_logger('gitlab-releases.log')


class GitLabReleaseManager:
    """Manages GitLab release operations."""

    def __init__(self, api: 'GitLabAPI'):
        """Initialize with GitLab API instance.

        Args:
            api: GitLabAPI instance for making requests
        """
        self.api = api
        self.project_root = api.project_root
        self.project_path = api.project_path
        self.base_url = api.base_url
        self.raw_project_path = api.raw_project_path
        self.api_base = api.api_base
        self.api_token = api.api_token

    def _upload_release_asset(self, file_path: str) -> Optional[Dict]:
        """Upload file and return link for release assets.

        Args:
            file_path: Path to file to upload

        Returns:
            Dict with 'name', 'url' keys for assets.links, or None on error
        """
        if requests is None:  # cold session without venv
            _log("Warning: 'requests' not installed; cannot upload release asset.")
            return None

        # Validate file path
        file = Path(file_path).resolve()
        if not file.exists():
            raise ValueError(f"Asset file not found: {file_path}")

        if not file.is_file():
            raise ValueError(f"Asset path is not a file: {file_path}")

        # SECURITY: Prevent path traversal - ensure file is within project boundaries
        project_root_resolved = Path(self.project_root).resolve()
        try:
            file.relative_to(project_root_resolved)
        except ValueError:
            raise ValueError(f"Asset path outside project directory: {file_path}")

        # Check file size (GitLab has 10GB project upload limit)
        size_mb = file.stat().st_size / (1024 * 1024)
        if size_mb > 10240:  # 10GB in MB
            raise ValueError(f"Asset too large: {size_mb:.1f}MB (max: 10240MB)")

        try:
            # Read file as binary
            with open(file, 'rb') as f:
                files = {'file': (file.name, f, 'application/octet-stream')}

                # Upload to GitLab uploads endpoint
                url = f"{self.api_base}/projects/{self.project_path}/uploads"
                response = requests.post(url, headers={'PRIVATE-TOKEN': self.api_token}, files=files, timeout=60)

                response.raise_for_status()
                upload_data = response.json()

                # Construct full URL from response
                upload_url = upload_data.get('url', '')
                if upload_url.startswith('/'):
                    full_url = f"{self.base_url}/{self.raw_project_path}{upload_url}"
                else:
                    full_url = upload_url

                return {
                    'name': file.name,
                    'url': full_url
                }

        except requests.exceptions.RequestException as e:
            # Redact the token — this raw requests.post bypasses the base
            # _make_request HTTPError path that normally scrubs it.
            _log(self.api._redact_token(f"Warning: Failed to upload asset {file_path}: {e}"))
            return None
        except Exception as e:
            _log(self.api._redact_token(f"Warning: Unexpected error uploading asset {file_path}: {e}"))
            return None

    def get_release(self, tag_name: str) -> Optional[Dict]:
        """Get release details by tag name.

        Args:
            tag_name: Tag name to fetch

        Returns:
            Release data dict or None if not found
        """
        # Input validation
        if not tag_name or not isinstance(tag_name, str):
            raise ValueError("Tag name must be a non-empty string")

        if len(tag_name) > 128:
            raise ValueError("Tag name too long (max 128 characters)")

        # URL encode tag name
        import urllib.parse
        encoded_tag = urllib.parse.quote(tag_name, safe='')
        result = self.api._make_request('GET', f"/projects/{self.project_path}/releases/{encoded_tag}")

        # Handle 404 specifically - return None for not found
        if result and result.get('error') and result.get('status_code') == 404:
            return None
        return self.api._raise_on_error(result, "GitLab API error getting release")

    def create_release(self, tag_name: str, release_name: str, description: str,
                      asset_paths: List[str] = None) -> Optional[Dict]:
        """Create a GitLab release with optional file uploads.

        Args:
            tag_name: Git tag for release (e.g., "v1.0.0")
            release_name: Human-readable release name
            description: Release notes in markdown
            asset_paths: Optional list of file paths to upload

        Returns:
            Release data dict if successful, None if failed
        """
        # Input validation
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

        # SECURITY: Strict tag name validation
        TAG_SAFE_PATTERN = r'^[a-zA-Z0-9._-]+$'
        if not re.match(TAG_SAFE_PATTERN, tag_name.strip()):
            raise ValueError(f"Tag name contains invalid characters: {tag_name}")

        # Check if release already exists
        existing = self.get_release(tag_name)
        if existing:
            raise Exception(f"Release already exists for tag '{tag_name}'")

        # Create the tag locally if missing, then push it to origin.
        ensure_tag_created_and_pushed(tag_name, release_name, self.project_root, log=_log)

        # Upload assets if provided
        asset_links = []
        if asset_paths:
            if len(asset_paths) > 10:
                raise ValueError("Too many assets (max 10 per release)")

            for asset_path in asset_paths:
                link = self._upload_release_asset(asset_path)
                if link:
                    asset_links.append(link)
                else:
                    _log(f"Warning: Skipping failed asset upload: {asset_path}")

        # Prepare release data
        data = {
            'tag_name': tag_name.strip(),
            'name': release_name.strip(),
            'description': description or ''
        }

        # Add asset links if any were uploaded
        if asset_links:
            data['assets'] = {
                'links': asset_links
            }

        # Create release
        result = self.api._make_request('POST', f"/projects/{self.project_path}/releases", data)

        return self.api._raise_on_error(result, "GitLab API error creating release")
