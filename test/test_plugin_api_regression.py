#!/usr/bin/env python3
"""F1–F7 plugin-API regression suite (l-test-suite-ci — task 8/8 of the plugin conversion).

SINGLE AUDITABLE MAP from each load-bearing Claude-Code plugin-API fact (verified in
the h-poc-spike; see docs/brainstorm-results/plugin-conversion.md §8) to the contract
that guards it. The plugin API is young; when a future Claude Code release breaks one
of these assumptions, the matching F-test fails LOUDLY and names the fact.

Each group documents: (a) the assumption the plugin design relies on, (b) what this
test pins on OUR side, and (c) any part that is a live-host behavior pytest cannot
prove (verified once in the spike, not re-provable here). Some assertions deliberately
overlap dedicated test modules (test_path_resolution / test_shim / test_command_namespace
/ test_token_resolution / test_plugin_manifest_json) — the overlap is intentional: this
file is the consolidated F-fact index, the others are the per-subsystem suites.

  F1  hooks MERGE (not replace) across sources + multi-hook additionalContext order
  F2  CLAUDE_PROJECT_DIR is present in the hook/MCP env and points at the project
  F3  CLAUDE_PLUGIN_ROOT resolved at point-of-use (ephemeral; no frozen const);
      CLAUDE_PLUGIN_DATA is the only persistent root (no __file__ fallback)
  F4  SessionStart can only ADVISE; the PreToolUse companion does the hard block
  F5  userConfig stores the 4 sensitive tokens (keychain → CLAUDE_PLUGIN_OPTION_*),
      resolved env-first
  F6  ${CLAUDE_PLUGIN_ROOT}/${CLAUDE_PLUGIN_DATA} substitution + python3 launcher
      (with a Windows python/py fallback)
  F7  plugin slash commands namespace as /team-management:<bare-filename>

Run with: python3 -m pytest test/test_plugin_api_regression.py -v
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugin"
HOOKS_DIR = PLUGIN / "hooks"
HOOKS_JSON = HOOKS_DIR / "hooks.json"
MCP_JSON = PLUGIN / ".mcp.json"
PLUGIN_JSON = PLUGIN / ".claude-plugin" / "plugin.json"
COMMANDS = PLUGIN / "commands"
SESSION_START = HOOKS_DIR / "session-start.py"
SESSIONS_ENFORCE = HOOKS_DIR / "sessions-enforce.py"

sys.path.insert(0, str(HOOKS_DIR))
import shared_state  # noqa: E402
import _shim  # noqa: E402


def _hooks_obj():
    return json.loads(HOOKS_JSON.read_text(encoding="utf-8"))


def _all_hook_commands(obj):
    for entries in obj["hooks"].values():
        for entry in entries:
            for h in entry["hooks"]:
                yield h["command"]


# ===========================================================================
# F1 — hooks MERGE (not replace) + multi-hook additionalContext ordering
# ===========================================================================
# ASSUMPTION: Claude Code MERGES the plugin's hooks.json with the project's
# .claude/settings.json hooks (it does not replace one set with the other), and runs
# multiple hooks on the same event in a defined order.
# LIVE-HOST (not unit-testable): the cross-source merge itself is performed by
# Claude's loader and was verified in the spike — pytest cannot observe it.
# OUR SIDE (pinned here): the manifest is structured to rely on same-event
# coexistence. Behavioral guidance is NO LONGER concatenated into the session-start
# additionalContext string — it is delivered via @-includes in the project's
# CLAUDE.md (durable across /compact; h-durable-guidance-via-claude-md). This test
# pins the merge-not-replace manifest shape and that session-start WIRES (not injects)
# the guidance.

def test_f1_manifest_registers_multiple_hooks_per_event():
    hooks = _hooks_obj()["hooks"]
    # UserPromptSubmit carries TWO hooks (user-messages + config_intent_gate):
    # proof the design relies on same-event coexistence (merge-not-replace).
    submit = [h["command"] for entry in hooks["UserPromptSubmit"] for h in entry["hooks"]]
    assert len(submit) >= 2, submit
    # PreToolUse carries TWO matcher entries (sessions-enforce + task-transcript-link).
    assert len(hooks["PreToolUse"]) == 2, hooks["PreToolUse"]


def test_f1_session_start_wires_guidance_not_injected(tmp_path):
    """session-start delivers guidance via @-includes in CLAUDE.md (durable across
    /compact), NOT by injecting it into additionalContext. Pins the new contract:
    no '## team-management Behaviors' / '## Wiki Behaviors' headers in the context,
    and a managed @-block wired into CLAUDE.md (incl. @CLAUDE.wiki.md when enabled)."""
    proj = tmp_path / "proj"
    (proj / ".claude" / "state").mkdir(parents=True)
    (proj / "team-management").mkdir(parents=True)
    (proj / "team-management" / "config.json").write_text(
        json.dumps({"wiki": {"enabled": True}}), encoding="utf-8")
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(proj), "CLAUDE_PLUGIN_ROOT": str(PLUGIN)}
    payload = json.dumps({"hook_event_name": "SessionStart", "source": "startup"})
    r = subprocess.run([sys.executable, str(SESSION_START)], input=payload,
                       capture_output=True, text=True, cwd=str(proj), env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    ctx = json.loads(r.stdout.strip())["hookSpecificOutput"]["additionalContext"]
    assert "## team-management Behaviors" not in ctx
    assert "## Wiki Behaviors" not in ctx
    claude_md = (proj / "CLAUDE.md").read_text(encoding="utf-8")
    assert "team-management:begin" in claude_md
    assert "@CLAUDE.tm.md" in claude_md
    assert "@CLAUDE.wiki.md" in claude_md  # wiki was enabled


# ===========================================================================
# F2 — CLAUDE_PROJECT_DIR present in env, points at the project
# ===========================================================================
# ASSUMPTION (live-host, verified in spike): Claude Code sets CLAUDE_PROJECT_DIR in
# the hook + plugin-MCP-server process env. OUR SIDE: get_project_root() honours it.

def test_f2_get_project_root_honors_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    assert shared_state.get_project_root() == tmp_path


# ===========================================================================
# F3 — PLUGIN_ROOT resolved at point of use (no frozen const); PLUGIN_DATA-only
# ===========================================================================
# ASSUMPTION: CLAUDE_PLUGIN_ROOT is replaced on every plugin update (ephemeral), and
# CLAUDE_PLUGIN_DATA is the persistent root. CONTRACT: no import-time PLUGIN_ROOT
# constant (it would freeze a value set/changed later); persistent state must NOT
# fall back to __file__ under the ephemeral root.

def test_f3_no_frozen_plugin_root_constant():
    assert not hasattr(shared_state, "PLUGIN_ROOT"), \
        "an import-time PLUGIN_ROOT constant would ignore CLAUDE_PLUGIN_ROOT set later"
    assert callable(shared_state.get_plugin_root)


def test_f3_plugin_root_resolved_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    assert shared_state.get_plugin_root() == tmp_path


def test_f3_plugin_data_env_only_no_file_fallback(monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    assert shared_state.get_plugin_data() is None


# ===========================================================================
# F4 — SessionStart can only ADVISE; PreToolUse companion does the hard block
# ===========================================================================
# ASSUMPTION (live-host, verified in spike): SessionStart hooks cannot block a
# session. So a coexisting legacy install can only be WARNED about at session start;
# the real enforcement is the PreToolUse companion in sessions-enforce.py (exit 2).

def _legacy_project(tmp_path):
    """A project carrying a legacy install alongside an active task (for both the
    advisory and the companion-block checks)."""
    state = tmp_path / ".claude" / "state"
    state.mkdir(parents=True)
    (tmp_path / "team-management" / "tasks").mkdir(parents=True)
    (state / "daic-mode.json").write_text(json.dumps({"mode": "discussion"}))
    (state / "current_task.json").write_text(json.dumps(
        {"task": "m-x", "branch": "fix/x", "services": [], "updated": "2026-06-25"}))
    (tmp_path / "team-management" / "tasks" / "m-x.md").write_text(
        "---\ntask: m-x\nbranch: fix/x\nstatus: in-progress\n---\n# t\n")
    legacy_hooks = tmp_path / ".claude" / "hooks"
    legacy_hooks.mkdir(parents=True)
    (legacy_hooks / "sessions-enforce.py").write_text("# legacy\n")
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps({
        "hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": [
            {"type": "command", "command": "python3 .claude/hooks/sessions-enforce.py"}]}]}}))
    return tmp_path


def test_f4_session_start_advises_not_blocks(tmp_path):
    proj = _legacy_project(tmp_path)
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(proj), "CLAUDE_PLUGIN_ROOT": str(PLUGIN)}
    payload = json.dumps({"hook_event_name": "SessionStart", "source": "startup"})
    r = subprocess.run([sys.executable, str(SESSION_START)], input=payload,
                       capture_output=True, text=True, cwd=str(proj), env=env, timeout=30)
    # ADVISE: exit 0 (no block) + advisory text present.
    assert r.returncode == 0, r.stderr
    ctx = json.loads(r.stdout.strip())["hookSpecificOutput"]["additionalContext"]
    assert "[Boot Detector" in ctx


def test_f4_pretooluse_companion_blocks(tmp_path):
    proj = _legacy_project(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=str(proj), check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "--allow-empty", "-m", "i", "-q"], cwd=str(proj), check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "fix/x"], cwd=str(proj), check=True)
    # CLAUDE_PLUGIN_ROOT truthy → is_plugin_mode True → companion active.
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(proj), "CLAUDE_PLUGIN_ROOT": str(proj)}
    payload = json.dumps({"tool_name": "Edit", "tool_input": {
        "file_path": str(proj / "src.py"), "old_string": "a", "new_string": "b"}})
    r = subprocess.run([sys.executable, str(SESSIONS_ENFORCE)], input=payload,
                       capture_output=True, text=True, cwd=str(proj), env=env, timeout=30)
    # BLOCK: exit 2 with the boot-detector reason.
    assert r.returncode == 2, f"expected block, got {r.returncode}; {r.stderr!r}"
    assert "[Boot Detector]" in r.stderr


# ===========================================================================
# F5 — per-project token file (userConfig / keychain retired)
# ===========================================================================
# The OS-keychain userConfig token model was retired (m-per-project-provider-tokens):
# it was global-per-plugin, so two projects could not use different tokens. The
# manifest declares NO userConfig, and resolve_provider_token reads the per-project
# .claude/state/provider-tokens.json file, then config — there is no env tier.
_PROVIDERS = {"gitlab", "jira", "github", "telegram"}


def test_f5_manifest_has_no_userconfig_tokens():
    """The keychain userConfig block is gone — tokens live in a per-project file the
    AI cannot read, not the global-per-plugin OS keychain."""
    manifest = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))
    assert "userConfig" not in manifest, "userConfig (keychain tokens) must be removed"


def test_f5_provider_token_env_covers_four_providers():
    """The resolver still knows the four providers (used to enumerate the seed
    template and to read the legacy env-name key as a back-compat fallback)."""
    assert set(shared_state._PROVIDER_TOKEN_ENV) == _PROVIDERS


def test_f5_resolve_provider_token_file_then_config(monkeypatch, tmp_path):
    """File tier wins over config; env is NOT a source (the keychain tier is gone)."""
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    state = tmp_path / ".claude" / "state"
    state.mkdir(parents=True)
    # An env var may be present but MUST be ignored — there is no env tier.
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_GITLAB_API_TOKEN", "env-tok")
    assert shared_state.resolve_provider_token("gitlab", "cfg-tok") == "cfg-tok"
    # A token in the per-project file wins over config.
    (state / "provider-tokens.json").write_text(
        json.dumps({"gitlab": "file-tok"}), encoding="utf-8")
    assert shared_state.resolve_provider_token("gitlab", "cfg-tok") == "file-tok"


# ===========================================================================
# F6 — ${CLAUDE_PLUGIN_ROOT}/${CLAUDE_PLUGIN_DATA} substitution + python3 launcher
# ===========================================================================
# ASSUMPTION (live-host, verified in spike): Claude Code substitutes
# ${CLAUDE_PLUGIN_ROOT} / ${CLAUDE_PLUGIN_DATA} in hooks.json / .mcp.json and a
# `python3` launcher resolves. OUR SIDE: every command uses the placeholder + the
# python3 launcher; the shim picks the data-dir venv and falls back on Windows.

def test_f6_hook_commands_use_python3_and_plugin_root():
    for cmd in _all_hook_commands(_hooks_obj()):
        assert cmd.startswith("python3 "), f"hook command not python3-launched: {cmd!r}"
        assert "${CLAUDE_PLUGIN_ROOT}" in cmd, cmd
        assert "_shim.py" in cmd, cmd


def test_f6_mcp_command_uses_python3_and_bootstrap():
    server = json.loads(MCP_JSON.read_text(encoding="utf-8"))["mcpServers"]["tm"]
    assert server["command"] == "python3", server
    args = " ".join(server.get("args", []))
    assert "${CLAUDE_PLUGIN_ROOT}" in args and "bootstrap_mcp.py" in args, args


def test_f6_shim_selects_venv_under_plugin_data(tmp_path, monkeypatch):
    venv = tmp_path / "venv"
    vp = venv / ("Scripts/python.exe" if _shim.os.name == "nt" else "bin/python")
    vp.parent.mkdir(parents=True)
    vp.write_text("")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    assert _shim.venv_python_path() == vp


def test_f6_shim_windows_fallback():
    which = {"python": "C:/Python/python.exe", "py": "C:/Windows/py.exe"}.get
    assert _shim.system_python(os_name="nt", which=which, executable="") == "C:/Python/python.exe"


# ===========================================================================
# F7 — plugin slash commands namespace as /team-management:<bare-filename>
# ===========================================================================
# ASSUMPTION (live-host, verified in spike): plugin commands namespace as
# /team-management:<filename-without-.md> and the command_source=="plugin" expansion
# event carries command_name. OUR SIDE: bare filenames + the intent-gate matches the
# namespaced name on the structural path (and ignores a prose mention).
_EXPECTED_COMMANDS = {
    "config.md", "init.md", "clean-check.md",
    "wiki-ingest.md", "wiki-lint.md", "wiki-tune.md",
    "custom-protocol-create.md", "custom-protocol-update-after-reinstall.md",
}


def test_f7_commands_are_bare_named():
    actual = {p.name for p in COMMANDS.glob("*.md")}
    assert actual == _EXPECTED_COMMANDS, actual
    assert not any(n.startswith("tm-") for n in actual), actual


def test_f7_intent_gate_recognizes_namespaced_command():
    import config_intent_gate as gate
    assert "team-management:config" in gate._COMMAND_NAMES
    # PRIMARY structural expansion payload fires.
    assert gate._should_fire(
        {"command_source": "plugin", "command_name": "team-management:config"})
    # A mere prose mention must NOT fire.
    assert not gate._should_fire({"prompt": "tell me about /team-management:config"})
