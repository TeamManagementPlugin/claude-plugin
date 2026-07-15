#!/usr/bin/env python3
"""m-enforcement-and-git-hardening — session-start.py hardening.

- directory-style tasks (tasks/<task>/README.md) load their context at session
  start (only tasks/<task>.md was loaded before).
- the flag-clearing unlink chain survives an OSError — a locked/undeletable flag
  must not kill SessionStart (bare .unlink() would raise).
- the compact-pending.flag restoration checkpoint is PRESERVED on `resume`
  (extending the SessionStart matcher to include resume must not erase a
  not-yet-restored post-compact checkpoint) but CLEARED on a fresh startup.
- the hooks.json SessionStart matcher includes `resume`.

Run with: python3 -m pytest test/test_session_start_hardening.py -v
"""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "plugin"
HOOK = PLUGIN_DIR / "hooks" / "session-start.py"


def _project(tmp_path, task=None):
    (tmp_path / ".claude" / "state").mkdir(parents=True)
    tm = tmp_path / "team-management"
    (tm / "tasks").mkdir(parents=True)
    (tm / "config.json").write_text(
        json.dumps({"wiki": {"enabled": False}}), encoding="utf-8")
    state = tmp_path / ".claude" / "state"
    task_state = {"task": task, "branch": "fix/x" if task else None,
                  "services": [], "updated": "2026-07-04"}
    (state / "current_task.json").write_text(json.dumps(task_state), encoding="utf-8")
    (state / "daic-mode.json").write_text(
        json.dumps({"mode": "discussion"}), encoding="utf-8")
    return tmp_path


def _run(project, source="startup"):
    env = {**os.environ,
           "CLAUDE_PROJECT_DIR": str(project),
           "CLAUDE_PLUGIN_ROOT": str(PLUGIN_DIR)}
    payload = json.dumps({"hook_event_name": "SessionStart", "source": source})
    return subprocess.run([sys.executable, str(HOOK)], input=payload,
                          capture_output=True, text=True, cwd=str(project),
                          env=env, timeout=25)


def _additional_context(result):
    assert result.returncode == 0, f"session-start failed: {result.stderr!r}"
    return json.loads(result.stdout.strip())["hookSpecificOutput"]["additionalContext"]


def test_directory_task_readme_loaded(tmp_path):
    project = _project(tmp_path, task="m-dir")
    readme = project / "team-management" / "tasks" / "m-dir" / "README.md"
    readme.parent.mkdir(parents=True, exist_ok=True)
    readme.write_text(
        "---\ntask: m-dir\nbranch: fix/x\nstatus: in-progress\n---\n\n"
        "# Dir task\nDIRECTORY_TASK_SENTINEL_XYZ\n", encoding="utf-8")
    ctx = _additional_context(_run(project))
    assert "DIRECTORY_TASK_SENTINEL_XYZ" in ctx, \
        "directory-task README not loaded at session start"


def test_unlink_permission_error_survived(tmp_path):
    project = _project(tmp_path)
    # A flag that is actually a DIRECTORY makes .unlink() raise (IsADirectoryError
    # / PermissionError — both OSError). _safe_unlink must swallow it; a bare
    # .unlink() would crash SessionStart.
    (project / ".claude" / "state" / "context-warning-80.flag").mkdir()
    r = _run(project)
    assert r.returncode == 0, f"unlink OSError killed SessionStart: {r.stderr!r}"


def test_resume_preserves_compact_pending_flag(tmp_path):
    project = _project(tmp_path)
    flag = project / ".claude" / "state" / "compact-pending.flag"
    flag.write_text(json.dumps({"task": "m-x"}), encoding="utf-8")
    r = _run(project, source="resume")
    assert r.returncode == 0, r.stderr
    assert flag.exists(), \
        "resume erased the not-yet-restored post-compact checkpoint"


def test_startup_clears_compact_pending_flag(tmp_path):
    project = _project(tmp_path)
    flag = project / ".claude" / "state" / "compact-pending.flag"
    flag.write_text(json.dumps({"task": "m-x"}), encoding="utf-8")
    r = _run(project, source="startup")
    assert r.returncode == 0, r.stderr
    assert not flag.exists(), "startup should clear a stale compact-pending flag"


def test_hooks_json_sessionstart_includes_resume():
    manifest = json.loads((PLUGIN_DIR / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    session_start = manifest.get("hooks", {}).get("SessionStart", [])
    matchers = [h.get("matcher", "") for h in session_start]
    assert any("resume" in m.split("|") for m in matchers), \
        f"SessionStart matcher must include resume: {matchers}"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
