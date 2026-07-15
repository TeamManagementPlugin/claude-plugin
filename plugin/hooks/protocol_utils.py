#!/usr/bin/env python3
"""
Protocol Utilities for Code Review Enforcement

Provides helper functions for config-driven code review enforcement
in task completion and feature development protocols.
"""
import json
import re
from typing import Dict

# get_project_root is single-sourced in shared_state (same hooks dir); re-exported
# here so existing protocol_utils.get_project_root callers keep working while the
# resolver logic lives in exactly one place (l-refactor-code-quality-cleanup). The
# env-first → .claude-walk semantics are identical; the former local copy also
# carried a max_depth=20 cap that shared_state does not — an untested edge case
# whose removal only helps (deeper trees now resolve instead of falling back).
from shared_state import get_project_root


def get_code_review_enforcement_config() -> Dict:
    """
    Load code review enforcement configuration with safe defaults.

    Returns:
        Configuration dictionary with code_review section.
        Defaults to enforce_warnings=False if config missing or malformed.
    """
    try:
        config_file = get_project_root() / "team-management" / "config.json"

        if not config_file.exists():
            # No config file - return safe default
            return {"code_review": {"enforce_warnings": False}}

        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)

        # Ensure code_review section exists with default
        if "code_review" not in config:
            config["code_review"] = {"enforce_warnings": False}
        elif "enforce_warnings" not in config["code_review"]:
            config["code_review"]["enforce_warnings"] = False

        return config

    except (json.JSONDecodeError, IOError, KeyError):
        # Any error reading config - return safe default
        return {"code_review": {"enforce_warnings": False}}


def check_code_review_warnings(review_text: str) -> Dict:
    """
    Parse code review text and return warning analysis.

    Searches for the markdown pattern:
        ## 🟡 Warnings (N)

    Args:
        review_text: Full code review markdown text

    Returns:
        Dictionary with:
            - has_warnings: bool - True if warnings found
            - warning_count: int - Number of warnings
            - warning_section: str or None - Full warning section text
    """
    if not review_text or not isinstance(review_text, str):
        return {
            "has_warnings": False,
            "warning_count": 0,
            "warning_section": None
        }

    try:
        # Look for warning section header: ## 🟡 Warnings (N)
        # Pattern handles variations in spacing and emoji
        warning_pattern = r'##\s*🟡\s*Warnings?\s*\((\d+)\)'
        match = re.search(warning_pattern, review_text, re.MULTILINE | re.IGNORECASE)

        if not match:
            # No warnings section found
            return {
                "has_warnings": False,
                "warning_count": 0,
                "warning_section": None
            }

        warning_count = int(match.group(1))

        if warning_count == 0:
            # Warnings section exists but says (0)
            return {
                "has_warnings": False,
                "warning_count": 0,
                "warning_section": None
            }

        # Extract the warning section text
        # Find from "## 🟡 Warnings" to next "##" heading or end
        lines = review_text.split('\n')
        warning_start_idx = None
        warning_section_lines = []

        for i, line in enumerate(lines):
            # Check if this is the warnings header
            if re.match(warning_pattern, line, re.IGNORECASE):
                warning_start_idx = i
                warning_section_lines.append(line)
                continue

            # If we've started collecting and hit another section, stop
            if warning_start_idx is not None:
                if re.match(r'^##\s+', line) and not re.match(warning_pattern, line):
                    # Found next section, stop collecting
                    break
                warning_section_lines.append(line)

        warning_section = '\n'.join(warning_section_lines).strip()

        return {
            "has_warnings": True,
            "warning_count": warning_count,
            "warning_section": warning_section if warning_section else None
        }

    except (ValueError, AttributeError, re.error):
        # Parsing error - fail safe (assume no warnings)
        return {
            "has_warnings": False,
            "warning_count": 0,
            "warning_section": None
        }


def get_review_completion_language(enforce_warnings: bool) -> Dict:
    """
    Return config-aware protocol language for code review enforcement.

    Args:
        enforce_warnings: Whether warning enforcement is enabled

    Returns:
        Dictionary with language variants:
            - must_pass_language: str - "MUST pass" vs "should pass"
            - fail_action: str - "REQUIRED" vs "recommended"
            - warning_prompt: str - What to show user when warnings exist
    """
    if enforce_warnings:
        return {
            "must_pass_language": "MUST pass code review with zero critical issues and acknowledged warnings",
            "fail_action": "REQUIRED to address or acknowledge",
            "warning_prompt": (
                "⚠️  Code review warnings found.\n"
                "\n"
                "You must choose how to proceed:\n"
                "  1. Fix warnings now and re-run code review\n"
                "  2. Acknowledge warnings and proceed anyway\n"
                "  3. Cancel task completion\n"
                "\n"
                "Your choice (1/2/3): "
            )
        }
    else:
        return {
            "must_pass_language": "should pass code review with zero critical issues",
            "fail_action": "recommended to address",
            "warning_prompt": (
                "ℹ️  Code review found warnings.\n"
                "(Warnings not enforced - task completion will proceed)\n"
            )
        }
