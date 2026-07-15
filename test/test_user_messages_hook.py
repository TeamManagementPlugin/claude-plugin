#!/usr/bin/env python3
"""Behavioral tests for the UserPromptSubmit hook `plugin/hooks/user-messages.py`.

The hook is intentionally NON-importable — it `json.load(sys.stdin)` at module
scope and can `sys.exit()` early (bypass). So each case runs it as a subprocess
with a stdin payload over a temp project, mirroring how Claude Code drives the
UserPromptSubmit hook (same pattern as test_sessions_enforce_daic.py /
test_hook_stdin_guards.py).

`test/test_hook_stdin_guards.py` already covers the malformed-stdin and
depth-reset paths; this file covers the behaviors that had ZERO coverage:
  * auto-compact trigger + auto-compact-triggered.flag
  * 80% / 90% token warnings + context-warning-{80,90}.flag
  * post-compact checkpoint restoration + compact-pending.flag consumption
  * workflow-bypass short-circuit (early return, no [PROTOCOL REQUIRED])
  * no-active-task branch hint

Token-threshold control: get_context_length_from_transcript sums a main-chain
`message.usage` (input + cache_read + cache_creation); get_model_context_limit
honors config `auto_compact.context_limit` FIRST, so setting it to 1000 makes the
percentage exactly tokens/10 — deterministic, no giant transcript needed.

Run with: python3 -m pytest test/test_user_messages_hook.py -v
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = REPO_ROOT / "plugin" / "hooks" / "user-messages.py"


def _make_project(tmp_path, config=None):
    state = tmp_path / ".claude" / "state"
    state.mkdir(parents=True)
    (tmp_path / "team-management" / "tasks").mkdir(parents=True)
    (state / "daic-mode.json").write_text(json.dumps({"mode": "discussion"}),
                                          encoding="utf-8")
    (state / "current_task.json").write_text(json.dumps(
        {"task": None, "branch": None, "services": [], "updated": None}),
        encoding="utf-8")
    if config is not None:
        (tmp_path / "team-management" / "config.json").write_text(
            json.dumps(config), encoding="utf-8")
    return tmp_path


def _env(project_root):
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project_root)}
    env.pop("CLAUDE_PLUGIN_ROOT", None)  # mirror test_hook_stdin_guards.py:41-48
    return env


def _run_hook(project_root, prompt="", transcript_path=None):
    payload = json.dumps({"prompt": prompt,
                          "transcript_path": str(transcript_path or "")})
    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=payload, capture_output=True, text=True,
        cwd=str(project_root), env=_env(project_root), timeout=20)
    ctx = ""
    if result.stdout.strip():
        try:
            ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        except (json.JSONDecodeError, KeyError):
            ctx = ""
    return result.returncode, ctx, result.stderr


def _transcript(tmp_path, tokens, model="claude-x"):
    """Write a 1-line transcript JSONL whose newest main-chain usage sums to
    `tokens`. Returns the file path."""
    line = {"message": {"usage": {"input_tokens": tokens,
                                  "cache_read_input_tokens": 0,
                                  "cache_creation_input_tokens": 0},
                        "model": model}}
    path = tmp_path / "transcript.jsonl"
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    return path


def _flag(project_root, name):
    return project_root / ".claude" / "state" / name


# ---------------------------------------------------------------- auto-compact

def test_auto_compact_trigger(tmp_path):
    """>= threshold with auto_compact enabled -> [AUTO-COMPACT] directive and the
    once-per-session flag file is written."""
    project = _make_project(tmp_path, {
        "auto_compact": {"enabled": True, "threshold": 85, "context_limit": 1000}})
    transcript = _transcript(tmp_path, 900)  # 90%
    rc, ctx, stderr = _run_hook(project, prompt="hi", transcript_path=transcript)
    assert rc == 0, f"stderr={stderr!r}"
    assert "[AUTO-COMPACT" in ctx, ctx
    assert _flag(project, "auto-compact-triggered.flag").exists()


def test_auto_compact_suppressed_when_flag_present(tmp_path):
    """The once-per-session guard: a pre-existing auto-compact-triggered.flag
    suppresses a repeat [AUTO-COMPACT] directive even above threshold."""
    project = _make_project(tmp_path, {
        "auto_compact": {"enabled": True, "threshold": 85, "context_limit": 1000}})
    _flag(project, "auto-compact-triggered.flag").touch()
    transcript = _transcript(tmp_path, 900)  # 90%, still above threshold
    rc, ctx, _ = _run_hook(project, prompt="hi", transcript_path=transcript)
    assert rc == 0
    assert "[AUTO-COMPACT" not in ctx, ctx


# ------------------------------------------------------------ 80 / 90 warnings

def test_80_warning(tmp_path):
    """auto_compact disabled isolates the 80% fallback warning branch."""
    project = _make_project(tmp_path, {
        "auto_compact": {"enabled": False, "context_limit": 1000}})
    transcript = _transcript(tmp_path, 820)  # 82%
    rc, ctx, _ = _run_hook(project, prompt="hi", transcript_path=transcript)
    assert rc == 0
    assert "[80% WARNING]" in ctx, ctx
    assert _flag(project, "context-warning-80.flag").exists()


def test_90_warning(tmp_path):
    project = _make_project(tmp_path, {
        "auto_compact": {"enabled": False, "context_limit": 1000}})
    transcript = _transcript(tmp_path, 920)  # 92%
    rc, ctx, _ = _run_hook(project, prompt="hi", transcript_path=transcript)
    assert rc == 0
    assert "[90% WARNING]" in ctx, ctx
    assert _flag(project, "context-warning-90.flag").exists()


# ------------------------------------------------------- post-compact restoration

def test_post_compact_restoration(tmp_path):
    """A present compact-pending.flag injects the restoration summary AND is
    consumed (unlinked) so it fires exactly once."""
    project = _make_project(tmp_path, {})
    flag = _flag(project, "compact-pending.flag")
    flag.write_text(json.dumps({
        "task": "m-x", "branch": "fix/x", "daic_mode": "implementation",
        "protocol": {"name": "task", "current_step": 1, "step_name": "implementation"},
        "services": [],
    }), encoding="utf-8")
    rc, ctx, _ = _run_hook(project, prompt="resume")
    assert rc == 0
    assert "[POST-COMPACT RESTORATION]" in ctx, ctx
    assert "Task: m-x" in ctx
    assert "Branch: fix/x" in ctx
    assert not flag.exists(), "compact-pending.flag must be consumed (unlinked)"


# ---------------------------------------------------------- workflow-bypass short-circuit

def test_workflow_bypass_short_circuit(tmp_path):
    """With bypass active the hook emits ONLY the bypass banner and returns early
    — no [PROTOCOL REQUIRED] injection."""
    project = _make_project(tmp_path, {})
    _flag(project, "workflow-bypass.json").write_text(
        json.dumps({"enabled": True, "reason": "test", "updated": "2026-07-14"}),
        encoding="utf-8")
    rc, ctx, _ = _run_hook(project, prompt="anything")
    assert rc == 0
    assert "[WORKFLOW BYPASS ACTIVE]" in ctx, ctx
    assert "[PROTOCOL REQUIRED]" not in ctx, "bypass must short-circuit before protocol enforcement"


# -------------------------------------------------------------- no-task branch hint

def test_no_task_branch_hint(tmp_path):
    """No active task but on a non-default branch -> the start-a-protocol hint."""
    project = _make_project(tmp_path, {})
    subprocess.run(["git", "init", "-q"], cwd=str(project), check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "--allow-empty", "-m", "init", "-q"],
                   cwd=str(project), check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "fix/x"],
                   cwd=str(project), check=True)
    rc, ctx, _ = _run_hook(project, prompt="hello")
    assert rc == 0
    assert "No active task, but on branch 'fix/x'" in ctx, ctx


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
