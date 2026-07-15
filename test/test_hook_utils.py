#!/usr/bin/env python3
"""hook_utils.normalize_command tests (h-hook-port-boot-detector, commit 3).

normalize_command lives in plugin/hooks/hook_utils.py so the boot-detector hook
can import it without any installer package (the installer was retired in
m-installer-retirement).

Run with: python3 -m pytest test/test_hook_utils.py -v
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugin" / "hooks"))

import hook_utils  # noqa: E402


def test_strips_quotes_and_normalizes_separators():
    assert hook_utils.normalize_command(r'"py" "C:\a\b.py"') == "py C:/a/b.py"


def test_empty_returns_empty():
    assert hook_utils.normalize_command("") == ""


def test_unbalanced_quotes_degrade_without_raising():
    # ValueError fallback to whitespace split — must not raise.
    assert hook_utils.normalize_command('py "unterminated') == "py unterminated"


# ---------------------------------------------------------------------------
# command_is_read_only (m-enforcement-and-git-hardening) — word-boundary match
# for the sessions-enforce read-only Bash fast-path. A prior
# `part.startswith(prefix)` false-matched write-capable commands sharing a
# prefix with a read-only allowlist entry.
# ---------------------------------------------------------------------------

# The subset of DEFAULT_CONFIG.read_only_bash_commands the tests exercise,
# including the multi-token entries that a naive first-token match would break.
_READ_ONLY = ["ls", "cd", "cat", "grep", "git status", "git log", "sed -n"]


def test_bare_command_matches():
    assert hook_utils.command_is_read_only("ls", _READ_ONLY) is True
    assert hook_utils.command_is_read_only("cd", _READ_ONLY) is True


def test_prefix_followed_by_space_matches():
    assert hook_utils.command_is_read_only("ls -la", _READ_ONLY) is True
    assert hook_utils.command_is_read_only("cd /tmp", _READ_ONLY) is True
    assert hook_utils.command_is_read_only("cat file.txt", _READ_ONLY) is True


def test_multitoken_allowlist_entries_preserved():
    # The regression codex flagged: a naive first-token match would break these.
    assert hook_utils.command_is_read_only("git status --short", _READ_ONLY) is True
    assert hook_utils.command_is_read_only("git log --oneline -5", _READ_ONLY) is True
    assert hook_utils.command_is_read_only("sed -n '1,20p' f", _READ_ONLY) is True


def test_prefix_sharing_writes_are_not_read_only():
    # The whole point of the fix: these must NOT be classified read-only.
    assert hook_utils.command_is_read_only("cdk deploy", _READ_ONLY) is False
    assert hook_utils.command_is_read_only("catalog-cli push", _READ_ONLY) is False
    assert hook_utils.command_is_read_only("lsof", _READ_ONLY) is False


def test_chain_all_read_only():
    assert hook_utils.command_is_read_only("cat a && ls -l", _READ_ONLY) is True


def test_chain_with_one_write_segment_is_not_read_only():
    # A single non-read-only segment poisons the whole chain.
    assert hook_utils.command_is_read_only("cat a && cdk deploy", _READ_ONLY) is False


def test_empty_segments_skipped():
    # Trailing separator / whitespace-only segments must not flip the result.
    assert hook_utils.command_is_read_only("ls ; ", _READ_ONLY) is True


# ---------------------------------------------------------------------------
# is_task_markdown_path (m-enforcement-and-git-hardening) — Windows-tolerant
# task-file-edit detection for post-tool-use.py.
# ---------------------------------------------------------------------------

def test_task_markdown_forward_slash():
    assert hook_utils.is_task_markdown_path("team-management/tasks/m-x.md") is True
    assert hook_utils.is_task_markdown_path("/abs/team-management/tasks/m-x/README.md") is True


def test_task_markdown_backslash_windows():
    # The bug: the bare substring check was silently dead on backslash paths.
    assert hook_utils.is_task_markdown_path(r"team-management\tasks\m-x.md") is True
    assert hook_utils.is_task_markdown_path(r"C:\proj\team-management\tasks\m-x.md") is True


def test_non_task_paths_rejected():
    assert hook_utils.is_task_markdown_path("src/app/tasks/worker.py") is False
    assert hook_utils.is_task_markdown_path("team-management/tasks/m-x.txt") is False
    assert hook_utils.is_task_markdown_path("") is False
    assert hook_utils.is_task_markdown_path(None) is False
