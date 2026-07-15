"""
Release creation MCP tools.

Provides tools for creating releases on GitLab and GitHub with asset uploads.
"""

from typing import Dict, Any, List

from core.config import load_config
from core.project import setup_provider_imports


def register_tools(mcp):
    """Register release tools with the FastMCP server."""

    @mcp.tool()
    def release_create(tag_name: str, release_name: str, description: str,
                      asset_paths: List[str] = None) -> Dict[str, Any]:
        """
        Create a release on configured providers with optional asset uploads.

        Supports creating releases on GitLab and/or GitHub independently
        of the active issue tracking provider. If both are enabled, creates
        releases on both platforms.

        Args:
            tag_name: Git tag (e.g., "v1.0.0")
            release_name: Human-readable release name
            description: Release notes in markdown
            asset_paths: Optional list of absolute file paths to upload

        Returns:
            Dict with success status, provider results, and any errors
        """
        try:
            # Input validation
            if not tag_name or not isinstance(tag_name, str):
                return {
                    "success": False,
                    "error": "Tag name must be a non-empty string"
                }

            if not release_name or not isinstance(release_name, str):
                return {
                    "success": False,
                    "error": "Release name must be a non-empty string"
                }

            if asset_paths and not isinstance(asset_paths, list):
                return {
                    "success": False,
                    "error": "Asset paths must be a list of file paths"
                }

            # Check which providers are enabled
            config = load_config()
            gitlab_enabled = config.get('gitlab', {}).get('enabled', False)
            github_enabled = config.get('github', {}).get('enabled', False)

            if not gitlab_enabled and not github_enabled:
                return {
                    "success": False,
                    "error": "No release providers enabled. Enable GitLab or GitHub in config.json"
                }

            # Ensure provider imports are set up
            setup_provider_imports()

            result = {
                "success": True,
                "providers_attempted": [],
                "errors": []
            }

            # Create release on GitLab if enabled
            if gitlab_enabled:
                result["providers_attempted"].append("gitlab")
                try:
                    from gitlab_utils import GitLabAPI, GitLabReleaseManager
                    gitlab = GitLabAPI()

                    gitlab_release = GitLabReleaseManager(gitlab).create_release(
                        tag_name=tag_name,
                        release_name=release_name,
                        description=description,
                        asset_paths=asset_paths
                    )

                    if gitlab_release:
                        result["gitlab"] = {
                            "success": True,
                            "tag_name": gitlab_release.get('tag_name'),
                            "release_url": gitlab_release.get('_links', {}).get('self'),
                            "assets_count": len(gitlab_release.get('assets', {}).get('links', []))
                        }
                    else:
                        result["gitlab"] = {
                            "success": False,
                            "error": "GitLab release creation returned no data"
                        }
                        result["errors"].append("GitLab: Release creation failed")

                except Exception as e:
                    result["gitlab"] = {
                        "success": False,
                        "error": str(e)
                    }
                    result["errors"].append(f"GitLab: {str(e)}")

            # Create release on GitHub if enabled
            if github_enabled:
                result["providers_attempted"].append("github")
                try:
                    from github_utils import GitHubAPI, GitHubReleaseManager
                    github = GitHubAPI()

                    github_release = GitHubReleaseManager(github).create_release(
                        tag_name=tag_name,
                        release_name=release_name,
                        description=description,
                        asset_paths=asset_paths,
                        draft=False,
                        prerelease=False
                    )

                    if github_release:
                        result["github"] = {
                            "success": True,
                            "tag_name": github_release.get('tag_name'),
                            "release_url": github_release.get('html_url'),
                            "release_id": github_release.get('id'),
                            "assets_count": len(asset_paths) if asset_paths else 0
                        }
                    else:
                        result["github"] = {
                            "success": False,
                            "error": "GitHub release creation returned no data"
                        }
                        result["errors"].append("GitHub: Release creation failed")

                except Exception as e:
                    result["github"] = {
                        "success": False,
                        "error": str(e)
                    }
                    result["errors"].append(f"GitHub: {str(e)}")

            # Overall success if at least one provider succeeded
            gitlab_success = result.get("gitlab", {}).get("success", False)
            github_success = result.get("github", {}).get("success", False)
            result["success"] = gitlab_success or github_success

            return result

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "providers_attempted": []
            }
