#!/usr/bin/env python3
"""Plugin-runtime statusline tests (m-namespace-rename, commit 2).

`plugin/templates/statusline.py` gained a `CLAUDE_PLUGIN_ROOT` branch so it runs
in the plugin world: it imports `shared_state` from `${CLAUDE_PLUGIN_ROOT}/hooks`
and takes PROJECT_ROOT from shared_state (env-first -> CLAUDE_PROJECT_DIR), the
same root `get_protocol_state` / `load_protocol_config` read internally.

These run the script as a SUBPROCESS under a controlled, minimal environment
(plugin env vars set, PYTHONPATH and stray CLAUDE_* removed, cwd deliberately
DIFFERENT from CLAUDE_PROJECT_DIR) and assert:
  - the current DAIC mode renders (the SC#5 hard requirement) — proving the
    script reads daic-mode.json from CLAUDE_PROJECT_DIR, not cwd;
  - a sentinel protocol block renders (`task 2/5 ...`) — proving shared_state's
    PROJECT_ROOT (current_task.json) AND get_plugin_root() (task.json) BOTH
    resolve correctly, i.e. no split-root drift (codex review strengthening).

The temp project deliberately has NO `.claude/hooks/`, so if the plugin branch
were ever reordered after the legacy `CLAUDE_PROJECT_DIR` branch, the legacy
`from hooks.shared_state import ...` would ImportError (rc != 0) instead of
silently passing through the wrong path.

The real in-session render (does Claude Code expand ${CLAUDE_PLUGIN_ROOT} in a
settings.json statusLine command) is verified on a live plugin install in
m-plugin-verification (#6).

Run with: python3 -m pytest test/test_statusline_plugin.py -v
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO / "plugin"
STATUSLINE = PLUGIN_ROOT / "templates" / "statusline.py"


def _run(tmp_path, mode, protocol=None, plugin_root_env=True, config=None):
    """Run statusline.py under plugin env vars; return CompletedProcess.

    `mode` -> daic-mode.json. `protocol` (dict) -> current_task.json["protocol"].
    `config` (dict) -> team-management/config.json (statusline reads icon_style
    and project_name from it). cwd is a sibling dir, NOT the project, so a
    cwd-derived root would diverge.
    `plugin_root_env=False` simulates how Claude Code actually invokes a
    settings.json statusLine command: CLAUDE_PLUGIN_ROOT is NOT injected, so the
    script must self-locate its hooks via __file__ (it ships at <plugin>/templates/).
    """
    proj = tmp_path / "proj"
    state = proj / ".claude" / "state"
    state.mkdir(parents=True)
    (state / "daic-mode.json").write_text(json.dumps({"mode": mode}), encoding="utf-8")
    if protocol is not None:
        (state / "current_task.json").write_text(
            json.dumps({"task": "m-x", "protocol": protocol}), encoding="utf-8"
        )
    if config is not None:
        tm = proj / "team-management"
        tm.mkdir(parents=True, exist_ok=True)
        (tm / "config.json").write_text(json.dumps(config), encoding="utf-8")

    workdir = tmp_path / "elsewhere"
    workdir.mkdir()

    # Minimal, controlled environment: only what the script legitimately needs.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "CLAUDE_PROJECT_DIR": str(proj),
        # deliberately NO PYTHONPATH, NO CLAUDE_PLUGIN_DATA, NO other CLAUDE_*.
    }
    if plugin_root_env:
        env["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_ROOT)
    payload = json.dumps({
        "model": {"display_name": "Opus", "id": "claude-opus-4-8"},
        "session_id": "s-test",
        "cwd": str(workdir),
        "transcript_path": "",
    })
    return subprocess.run(
        [sys.executable, str(STATUSLINE)],
        input=payload, capture_output=True, text=True,
        env=env, cwd=str(workdir), timeout=30,
    )


def test_plugin_mode_renders_implementation(tmp_path):
    r = _run(tmp_path, "implementation")
    assert r.returncode == 0, r.stderr
    assert "Implement" in r.stdout, r.stdout


def test_plugin_mode_renders_discussion(tmp_path):
    r = _run(tmp_path, "discussion")
    assert r.returncode == 0, r.stderr
    assert "Discuss" in r.stdout, r.stdout


def test_plugin_mode_renders_documentation(tmp_path):
    r = _run(tmp_path, "documentation")
    assert r.returncode == 0, r.stderr
    assert "Document" in r.stdout, r.stdout


def test_daic_read_uses_project_root_not_cwd(tmp_path):
    """cwd != CLAUDE_PROJECT_DIR; the implementation mode only renders if the
    script reads daic-mode.json from the project root (not its cwd, which has no
    state dir and would default to 'discussion')."""
    r = _run(tmp_path, "implementation")
    assert r.returncode == 0, r.stderr
    assert "Implement" in r.stdout
    assert "Discuss" not in r.stdout  # would appear if it defaulted off cwd


def test_protocol_state_proves_root_alignment(tmp_path):
    """A sentinel protocol block renders only if shared_state's PROJECT_ROOT
    (reads current_task.json) AND get_plugin_root() (loads task.json for the step
    count) both resolve correctly — the split-root regression guard."""
    r = _run(tmp_path, "implementation",
             protocol={"name": "task", "current_step": 1, "step_name": "implementation"})
    assert r.returncode == 0, r.stderr
    # task protocol has 5 steps; current_step 1 -> "2/5".
    assert "task" in r.stdout
    assert "2/5" in r.stdout, r.stdout


# --- Plugin install WITHOUT CLAUDE_PLUGIN_ROOT in env (m-fix-plugin-mode-install-bugs) ---
# How Claude Code actually invokes a settings.json statusLine command: the plugin
# env vars are NOT injected. The script must self-locate its hooks dir via __file__
# (it ships at <plugin>/templates/) instead of falling to the legacy
# `from hooks.shared_state` branch, which crashes (no project .claude/hooks/).

def test_renders_without_plugin_root_env(tmp_path):
    r = _run(tmp_path, "implementation", plugin_root_env=False)
    assert r.returncode == 0, r.stderr
    assert "Implement" in r.stdout, r.stdout


def test_no_disabled_in_plugin_import_without_env(tmp_path):
    """A plugin-imported statusline must NOT show Mode: DISABLED merely because
    the project .claude/settings.json carries no hook entries — plugin-mode hooks
    live in the plugin's hooks.json, not in project settings."""
    r = _run(tmp_path, "discussion", plugin_root_env=False)
    assert r.returncode == 0, r.stderr
    assert "DISABLED" not in r.stdout, r.stdout
    assert "Discuss" in r.stdout, r.stdout


# --- Project-name segment (m-statusline-project-name) ---
# Line 2 carries a project label between the open-tasks segment and MCP:
# configured `project_name` wins; empty/unset/whitespace falls back to the
# project folder name (PROJECT_ROOT.name). ASCII icon style renders "Project: ".

def test_project_name_from_config(tmp_path):
    r = _run(tmp_path, "discussion", config={"project_name": "my-cool-app"})
    assert r.returncode == 0, r.stderr
    assert "my-cool-app" in r.stdout, r.stdout


def test_project_name_fallback_to_folder(tmp_path):
    # No config file at all -> falls back to the project folder name ("proj").
    r = _run(tmp_path, "discussion")
    assert r.returncode == 0, r.stderr
    assert "Project: proj" in r.stdout, r.stdout


def test_project_name_whitespace_falls_back(tmp_path):
    # A whitespace-only value is treated as unset -> folder-name fallback.
    r = _run(tmp_path, "discussion", config={"project_name": "   "})
    assert r.returncode == 0, r.stderr
    assert "Project: proj" in r.stdout, r.stdout


def test_project_name_nonstring_falls_back(tmp_path):
    # A non-string value is treated as unset -> folder-name fallback.
    r = _run(tmp_path, "discussion", config={"project_name": 123})
    assert r.returncode == 0, r.stderr
    assert "Project: proj" in r.stdout, r.stdout


def test_malformed_nondict_config_falls_back(tmp_path):
    # A valid-but-non-dict JSON root must not crash the statusline.
    r = _run(tmp_path, "discussion", config=[])
    assert r.returncode == 0, r.stderr
    assert "Project: proj" in r.stdout, r.stdout


def test_project_segment_between_open_and_mcp(tmp_path):
    # Order on line 2 must be: open tasks | <project> | MCP.
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"team-management": {}}}), encoding="utf-8"
    )
    r = _run(tmp_path, "discussion", config={"project_name": "zeta-app"})
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "zeta-app" in out, out
    # MCP segment renders the bare server name (no fake "(✓)" marker anymore).
    assert out.index("open") < out.index("zeta-app") < out.index("team-management"), out


# --- Honest MCP segment (m-statusline-and-test-infra, SC#2) ---
# In plugin mode the plugin's own `tm`/team-management server is surfaced under
# its full name even without a project .mcp.json, and the fake "(✓) connected"
# marker is gone (config-file reads cannot verify a live connection).

def test_plugin_server_shown_without_project_mcp_json(tmp_path):
    r = _run(tmp_path, "discussion")  # plugin_root_env=True, no project .mcp.json
    assert r.returncode == 0, r.stderr
    assert "MCP: team-management" in r.stdout, r.stdout


def test_no_fake_connected_checkmark(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"team-management": {}, "other": {}}}), encoding="utf-8"
    )
    r = _run(tmp_path, "discussion")
    assert r.returncode == 0, r.stderr
    assert "(✓)" not in r.stdout, r.stdout
    # dedupe: the plugin server and the same-named project entry collapse to one.
    assert r.stdout.count("team-management") == 1, r.stdout


def test_file_self_locate_guard_protects_legacy_copy():
    """The __file__ self-locate branch is guarded on parent.name == 'templates'
    so it CANNOT hijack the legacy deployed copy (team-management/statusline.py,
    parent 'team-management'), which must keep falling through to its
    CLAUDE_PROJECT_DIR branch. Drift-guard: a refactor must not drop the guard."""
    src = STATUSLINE.read_text(encoding="utf-8")
    assert 'parent.name == "templates"' in src
