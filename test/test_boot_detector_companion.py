#!/usr/bin/env python3
"""Boot-detector PreToolUse companion tests (h-hook-port-boot-detector, commit 3).

The companion lives inline in sessions-enforce.py after the subagent bypass and
blocks write-tools (exit 2) when running AS A PLUGIN (CLAUDE_PLUGIN_ROOT set)
alongside a legacy install. It must:
  - block (exit 2, "[Boot Detector]") in plugin-mode with a legacy install;
  - NOT fire for subagents (they exit at the bypass above);
  - NOT fire when the project is clean;
  - NOT fire in legacy/dev mode (no CLAUDE_PLUGIN_ROOT) — no self-block.

Run with: python3 -m pytest test/test_boot_detector_companion.py -v
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "plugin" / "hooks" / "sessions-enforce.py"


def _project(tmp_path, *, daic="discussion", subagent_depth=0, legacy=True):
    state = tmp_path / ".claude" / "state"
    state.mkdir(parents=True)
    (tmp_path / "team-management" / "tasks").mkdir(parents=True)
    (state / "daic-mode.json").write_text(json.dumps({"mode": daic}))
    (state / "current_task.json").write_text(json.dumps(
        {"task": "m-x", "branch": "fix/x", "services": [], "updated": "2026-06-24"}))
    (tmp_path / "team-management" / "tasks" / "m-x.md").write_text(
        "---\ntask: m-x\nbranch: fix/x\nstatus: in-progress\n---\n# t\n")
    if subagent_depth:
        (state / "subagent-depth.json").write_text(json.dumps({"depth": subagent_depth}))
    if legacy:
        hooks = tmp_path / ".claude" / "hooks"
        hooks.mkdir(parents=True)
        (hooks / "sessions-enforce.py").write_text("# legacy\n")
        (tmp_path / ".claude" / "settings.json").write_text(json.dumps({
            "hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": [
                {"type": "command", "command": "python3 .claude/hooks/sessions-enforce.py"}]}]}}))
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "--allow-empty", "-m", "i", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "fix/x"], cwd=str(tmp_path), check=True)
    return tmp_path


def _run_edit(project, *, plugin_mode):
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project)}
    if plugin_mode:
        env["CLAUDE_PLUGIN_ROOT"] = str(project)  # truthy → is_plugin_mode True
    else:
        env.pop("CLAUDE_PLUGIN_ROOT", None)
    payload = json.dumps({"tool_name": "Edit",
                          "tool_input": {"file_path": str(project / "src.py"),
                                         "old_string": "a", "new_string": "b"}})
    return subprocess.run([sys.executable, str(HOOK)], input=payload,
                          capture_output=True, text=True, cwd=str(project),
                          env=env, timeout=20)


def test_blocks_legacy_in_plugin_mode(tmp_path):
    project = _project(tmp_path, legacy=True)
    r = _run_edit(project, plugin_mode=True)
    assert r.returncode == 2, f"expected exit 2, got {r.returncode}; {r.stderr!r}"
    assert "[Boot Detector]" in r.stderr


def test_does_not_fire_for_subagent(tmp_path):
    project = _project(tmp_path, legacy=True, subagent_depth=1)
    r = _run_edit(project, plugin_mode=True)
    assert "[Boot Detector]" not in r.stderr
    assert r.returncode == 0, f"subagent edit should pass the bypass, got {r.returncode}; {r.stderr!r}"


def test_does_not_fire_when_clean(tmp_path):
    project = _project(tmp_path, legacy=False)
    r = _run_edit(project, plugin_mode=True)
    assert "[Boot Detector]" not in r.stderr


def test_no_self_block_in_legacy_mode(tmp_path):
    # Legacy artifacts present but NOT plugin-mode (no CLAUDE_PLUGIN_ROOT): this is
    # exactly the dev/legacy checkout — the companion must stay a no-op.
    project = _project(tmp_path, legacy=True)
    r = _run_edit(project, plugin_mode=False)
    assert "[Boot Detector]" not in r.stderr
