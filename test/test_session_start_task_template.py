#!/usr/bin/env python3
"""SessionStart task-template deploy tests (h-fix-task-template-not-deployed).

The retired pip installer (#7) used to copy plugin/templates/TEMPLATE.md into a
project as team-management/tasks/TEMPLATE.md; nothing replaced it, yet many
protocol steps tell the agent to read that path for the standard task-file
format. session-start.py now self-heals it (ensure_task_template_deployed),
gated on the team-management/ dir ALREADY existing so an un-opted-in project is
never scaffolded.

These run session-start.py as a SUBPROCESS (matching test_session_start_statusline.py)
under controlled env vars and assert on the team-management/tasks/TEMPLATE.md it
writes:
  - deploys it (byte-identical to the plugin source) when team-management/ exists
    and the file is absent
  - never clobbers an existing (user-edited) copy
  - does NOT scaffold a project that has no team-management/ dir
  - does nothing outside plugin mode
  - degrades gracefully (hook still exits 0) when tasks/ is a regular file

Run with: python3 -m pytest test/test_session_start_task_template.py -v
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "plugin"
HOOK = PLUGIN_DIR / "hooks" / "session-start.py"

SOURCE_TEMPLATE = PLUGIN_DIR / "templates" / "TEMPLATE.md"


def _project(tmp_path, *, team_management=True):
    (tmp_path / ".claude" / "state").mkdir(parents=True)
    if team_management:
        (tmp_path / "team-management").mkdir()
    return tmp_path


def _deployed_template(project):
    return project / "team-management" / "tasks" / "TEMPLATE.md"


def _run(project, *, plugin_mode=True):
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project)}
    if plugin_mode:
        env["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_DIR)
    else:
        env.pop("CLAUDE_PLUGIN_ROOT", None)
    payload = json.dumps({"hook_event_name": "SessionStart", "source": "startup"})
    return subprocess.run([sys.executable, str(HOOK)], input=payload,
                          capture_output=True, text=True, cwd=str(project),
                          env=env, timeout=20)


def test_deploys_template_when_absent(tmp_path):
    project = _project(tmp_path)
    result = _run(project)
    assert result.returncode == 0, result.stderr
    dest = _deployed_template(project)
    assert dest.exists()
    # byte-identical to the plugin source (the helper byte-copies, not text-copies)
    assert dest.read_bytes() == SOURCE_TEMPLATE.read_bytes()


def test_does_not_clobber_existing_template(tmp_path):
    project = _project(tmp_path)
    dest = _deployed_template(project)
    dest.parent.mkdir(parents=True)
    dest.write_text("# my custom template\n", encoding="utf-8")
    result = _run(project)
    assert result.returncode == 0, result.stderr
    assert dest.read_text(encoding="utf-8") == "# my custom template\n"


def test_no_scaffold_without_team_management_dir(tmp_path):
    project = _project(tmp_path, team_management=False)
    result = _run(project)
    assert result.returncode == 0, result.stderr
    # the gate must not create team-management/ on an un-opted-in project
    assert not (project / "team-management").exists()


def test_noop_outside_plugin_mode(tmp_path):
    project = _project(tmp_path)
    result = _run(project, plugin_mode=False)
    assert result.returncode == 0, result.stderr
    assert not _deployed_template(project).exists()


def test_degrades_when_tasks_is_a_file(tmp_path):
    project = _project(tmp_path)
    # tasks/ is a regular file -> mkdir(parents)/write under it fails; the helper
    # must swallow the error and the hook must still exit 0.
    (project / "team-management" / "tasks").write_text("not a dir\n", encoding="utf-8")
    result = _run(project)
    assert result.returncode == 0, result.stderr
    assert (project / "team-management" / "tasks").is_file()
