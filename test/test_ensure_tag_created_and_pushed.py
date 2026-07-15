#!/usr/bin/env python3
"""Characterization tests for the shared ``ensure_tag_created_and_pushed`` helper
(l-refactor-code-quality-cleanup, Item 1).

The GitLab and GitHub release managers had near-identical inline tag-create/push
blocks. Before extracting them into a single ``git_operations`` helper, this file
pins the observable behaviour that the existing release tests do NOT assert
(they only prove mocked routing reaches the manager):

  - existing tag → skip ``git tag -a``, still push
  - missing tag → list → annotate → push, in order
  - release_name CR/LF collapsed to spaces in the ``-m`` message
  - tag_name stripped in the git commands
  - optional ``log`` callback fires (GitLab) / stays silent when None (GitHub)
  - ``CalledProcessError`` (bytes AND str stderr) → Exception("Failed to ...")
  - push ``TimeoutExpired`` → Exception("Timeout while pushing ... to remote")

Run with: python3 -m pytest test/test_ensure_tag_created_and_pushed.py -v
"""
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HOOKS_DIR = _REPO / "plugin" / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

import git_operations  # noqa: E402  (hooks/git_operations.py — single source of truth)


def _router(list_stdout="", fail_on=None, timeout_on=None, fail_stderr=b"remote rejected"):
    """Build a subprocess.run side_effect that records calls and routes by command.

    ``fail_on`` / ``timeout_on``: a command prefix (list) that raises
    CalledProcessError / TimeoutExpired. ``list_stdout``: stdout returned for
    ``git tag -l`` (non-empty == tag already exists locally).
    """
    calls = []

    def run(cmd, *a, **kw):
        calls.append(cmd)
        if timeout_on and cmd[: len(timeout_on)] == timeout_on:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 30))
        if fail_on and cmd[: len(fail_on)] == fail_on:
            raise subprocess.CalledProcessError(1, cmd, output=b"", stderr=fail_stderr)
        if cmd[:3] == ["git", "tag", "-l"]:
            return MagicMock(returncode=0, stdout=list_stdout, stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    return run, calls


def test_missing_tag_creates_and_pushes_in_order():
    run, calls = _router(list_stdout="")  # empty → tag does not exist
    with patch.object(git_operations.subprocess, "run", side_effect=run):
        git_operations.ensure_tag_created_and_pushed("v1.0.0", "Release One", "/proj")
    verbs = [c[:2] for c in calls]
    assert verbs == [["git", "tag"], ["git", "tag"], ["git", "push"]]
    assert calls[0][:3] == ["git", "tag", "-l"]
    assert calls[1][:3] == ["git", "tag", "-a"]
    assert calls[2] == ["git", "push", "origin", "v1.0.0"]


def test_existing_tag_skips_annotate_still_pushes():
    run, calls = _router(list_stdout="v1.0.0\n")  # non-empty → tag exists
    with patch.object(git_operations.subprocess, "run", side_effect=run):
        git_operations.ensure_tag_created_and_pushed("v1.0.0", "Release One", "/proj")
    assert [c[:3] for c in calls] == [["git", "tag", "-l"], ["git", "push", "origin"]]
    assert not any(c[:3] == ["git", "tag", "-a"] for c in calls), "must NOT re-annotate"


def test_release_name_crlf_sanitized_in_message():
    run, calls = _router(list_stdout="")
    with patch.object(git_operations.subprocess, "run", side_effect=run):
        git_operations.ensure_tag_created_and_pushed(
            "v2", "Line1\nLine2\r\nLine3", "/proj"
        )
    annotate = next(c for c in calls if c[:3] == ["git", "tag", "-a"])
    msg = annotate[annotate.index("-m") + 1]
    assert "\n" not in msg and "\r" not in msg
    assert msg == "Release Line1 Line2  Line3"


def test_tag_name_stripped_in_git_commands():
    run, calls = _router(list_stdout="")
    with patch.object(git_operations.subprocess, "run", side_effect=run):
        git_operations.ensure_tag_created_and_pushed("  v3  ", "Rel", "/proj")
    # The git commands operate on the stripped tag; the human message keeps raw.
    assert calls[0] == ["git", "tag", "-l", "v3"]
    assert "v3" in calls[1] and "  v3  " not in calls[1]
    assert calls[2] == ["git", "push", "origin", "v3"]


def test_log_callback_receives_progress_messages():
    run, _ = _router(list_stdout="")  # missing tag → full 4-message path
    seen = []
    with patch.object(git_operations.subprocess, "run", side_effect=run):
        git_operations.ensure_tag_created_and_pushed(
            "v4", "Rel", "/proj", log=seen.append
        )
    assert len(seen) == 4
    assert any("not found locally" in m for m in seen)
    assert any("Created tag" in m for m in seen)
    assert any("Pushing tag" in m for m in seen)
    assert any("Successfully pushed" in m for m in seen)


def test_no_log_callback_is_silent():
    run, calls = _router(list_stdout="")
    with patch.object(git_operations.subprocess, "run", side_effect=run):
        # log defaults to None (GitHub behaviour) — must not raise, must push.
        git_operations.ensure_tag_created_and_pushed("v5", "Rel", "/proj")
    assert any(c[:2] == ["git", "push"] for c in calls)


def test_called_process_error_bytes_stderr_raises():
    run, _ = _router(list_stdout="v6\n", fail_on=["git", "push"], fail_stderr=b"denied xyz")
    with patch.object(git_operations.subprocess, "run", side_effect=run):
        with pytest.raises(Exception) as ei:
            git_operations.ensure_tag_created_and_pushed("v6", "Rel", "/proj")
    assert "Failed to create/push tag 'v6'" in str(ei.value)
    assert "denied xyz" in str(ei.value)


def test_called_process_error_str_stderr_falls_back():
    # stderr as str (not bytes) → .decode raises AttributeError → narrowed
    # `except Exception` fallback uses str(e), never crashes the handler.
    run, _ = _router(list_stdout="v7\n", fail_on=["git", "push"], fail_stderr="text-stderr")
    with patch.object(git_operations.subprocess, "run", side_effect=run):
        with pytest.raises(Exception) as ei:
            git_operations.ensure_tag_created_and_pushed("v7", "Rel", "/proj")
    assert "Failed to create/push tag 'v7'" in str(ei.value)


def test_push_timeout_raises_to_remote():
    run, _ = _router(list_stdout="v8\n", timeout_on=["git", "push"])
    with patch.object(git_operations.subprocess, "run", side_effect=run):
        with pytest.raises(Exception) as ei:
            git_operations.ensure_tag_created_and_pushed("v8", "Rel", "/proj")
    assert "Timeout while pushing tag 'v8' to remote" in str(ei.value)
