#!/usr/bin/env python3
"""Config intent-gate hook tests (m-config-mcp-flow, commit 3).

Covers config_intent_gate.py (UserPromptExpansion primary + UserPromptSubmit
backup, plugin-mode gated) and the shared_state config-session flag round-trip,
including the cross-process path-agreement codex flagged (hook writes the flag in
one process; check_config_session_flag reads the same file).

Run with: python3 -m pytest test/test_config_intent_gate.py -v
"""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "plugin" / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import shared_state  # noqa: E402
import config_intent_gate  # noqa: E402


def _run_hook(monkeypatch, tmp_path, payload, plugin_mode=True):
    """Call the hook's main() in-process with a mocked stdin + env."""
    (tmp_path / ".claude" / "state").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    if plugin_mode:
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "plugin_install"))
    else:
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    config_intent_gate.main()


def _flag_path(tmp_path):
    return tmp_path / ".claude" / "state" / "config-session.flag"


# --------------------------------------------------------------------------
# Firing conditions
# --------------------------------------------------------------------------

def test_expansion_primary_writes_flag(monkeypatch, tmp_path):
    _run_hook(monkeypatch, tmp_path, {
        "command_source": "plugin",
        "command_name": "team-management:config",
        "prompt": "/team-management:config",
        "session_id": "sess-1",
    })
    assert _flag_path(tmp_path).exists()
    ok, _ = shared_state.check_config_session_flag()
    assert ok is True


def test_submit_backup_writes_flag(monkeypatch, tmp_path):
    _run_hook(monkeypatch, tmp_path, {"prompt": "/team-management:config",
                                      "session_id": "sess-2"})
    assert _flag_path(tmp_path).exists()


def test_submit_backup_with_args_writes_flag(monkeypatch, tmp_path):
    _run_hook(monkeypatch, tmp_path, {"prompt": "/team-management:config wiki"})
    assert _flag_path(tmp_path).exists()


def test_prose_mention_does_not_fire(monkeypatch, tmp_path):
    _run_hook(monkeypatch, tmp_path,
              {"prompt": "tell me about /team-management:config please"})
    assert not _flag_path(tmp_path).exists()


def test_other_plugin_command_does_not_fire(monkeypatch, tmp_path):
    _run_hook(monkeypatch, tmp_path, {
        "command_source": "plugin",
        "command_name": "team-management:wiki-lint",
        "prompt": "/team-management:wiki-lint",
    })
    assert not _flag_path(tmp_path).exists()


def test_init_expansion_writes_flag(monkeypatch, tmp_path):
    # /team-management:init opens the config window too, so init can drive
    # config_update (set wiki.enabled) — h-durable-guidance-via-claude-md.
    _run_hook(monkeypatch, tmp_path, {
        "command_source": "plugin",
        "command_name": "team-management:init",
        "prompt": "/team-management:init",
    })
    assert _flag_path(tmp_path).exists()


def test_init_raw_prompt_writes_flag(monkeypatch, tmp_path):
    _run_hook(monkeypatch, tmp_path, {"prompt": "/team-management:init"})
    assert _flag_path(tmp_path).exists()


def test_init_prose_mention_does_not_fire(monkeypatch, tmp_path):
    _run_hook(monkeypatch, tmp_path,
              {"prompt": "what does /team-management:init do?"})
    assert not _flag_path(tmp_path).exists()


def test_non_plugin_source_does_not_fire(monkeypatch, tmp_path):
    # A user-defined command named 'config' with no matching raw prompt.
    _run_hook(monkeypatch, tmp_path, {
        "command_source": "user",
        "command_name": "config",
        "prompt": "",
    })
    assert not _flag_path(tmp_path).exists()


def test_not_plugin_mode_no_op(monkeypatch, tmp_path):
    _run_hook(monkeypatch, tmp_path,
              {"prompt": "/team-management:config"}, plugin_mode=False)
    assert not _flag_path(tmp_path).exists()


def test_malformed_stdin_no_crash(monkeypatch, tmp_path):
    (tmp_path / ".claude" / "state").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "pi"))
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json{{"))
    config_intent_gate.main()  # must not raise
    assert not _flag_path(tmp_path).exists()


# --------------------------------------------------------------------------
# Flag round-trip (shared_state contract)
# --------------------------------------------------------------------------

def test_flag_roundtrip_fresh_ok(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    (tmp_path / ".claude" / "state").mkdir(parents=True)
    shared_state.write_config_session_flag(session_id="s1")
    ok, reason = shared_state.check_config_session_flag()
    assert ok is True and reason == "ok"


def test_flag_expired_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    (tmp_path / ".claude" / "state").mkdir(parents=True)
    shared_state.write_config_session_flag(ttl_seconds=-1)
    ok, reason = shared_state.check_config_session_flag()
    assert ok is False and "expired" in reason


def test_flag_session_mismatch_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    (tmp_path / ".claude" / "state").mkdir(parents=True)
    shared_state.write_config_session_flag(session_id="real-session")
    ok, reason = shared_state.check_config_session_flag(session_id="other-session")
    assert ok is False and "different session" in reason


def test_flag_missing_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    (tmp_path / ".claude" / "state").mkdir(parents=True)
    ok, reason = shared_state.check_config_session_flag()
    assert ok is False and "no active config session" in reason


def test_refresh_preserves_session_id(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    (tmp_path / ".claude" / "state").mkdir(parents=True)
    shared_state.write_config_session_flag(session_id="hook-sid")
    # config_update refreshes with preserve_session_id=True (no sid arg).
    shared_state.write_config_session_flag(preserve_session_id=True)
    data = json.loads(_flag_path(tmp_path).read_text(encoding="utf-8"))
    assert data["session_id"] == "hook-sid"


# --------------------------------------------------------------------------
# Cross-process path agreement (codex finding): hook writes in process A via the
# real script; check_config_session_flag in this process B reads the same file.
# --------------------------------------------------------------------------

def test_cross_process_flag_path_agreement(monkeypatch, tmp_path):
    (tmp_path / ".claude" / "state").mkdir(parents=True)
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(tmp_path)
    env["CLAUDE_PLUGIN_ROOT"] = str(tmp_path / "plugin_install")
    payload = json.dumps({"command_source": "plugin",
                          "command_name": "team-management:config",
                          "prompt": "/team-management:config"})
    # Process A: run the hook script directly.
    subprocess.run([sys.executable, str(HOOKS_DIR / "config_intent_gate.py")],
                   input=payload, text=True, env=env, check=True, timeout=15)
    # Process B (this process): the MCP-side checker resolves the SAME path.
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    ok, _ = shared_state.check_config_session_flag()
    assert ok is True, "hook-written flag not found by the MCP-side checker"
