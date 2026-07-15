#!/usr/bin/env python3
"""Targeted MCP-core tests (m-statusline-and-test-infra, SC#3/#4).

Replaces the retired `plugin/mcp/test_server.py`, which polluted every `pytest`
run (its `def test_*(env, results)` signatures were read as missing fixtures ->
11 collection ERRORS) and mutated the developer's LIVE `.claude/state` (its DAIC
test pointed `CLAUDE_PROJECT_DIR` at the real repo root). `test_mcp_tool_inventory.py`
remains the static registration/inventory drift-guard; this file covers the
handful of behaviours worth exercising at runtime:

  * provider detection (gitlab / jira / github / disabled)
  * config load
  * the disabled-provider `issue_status` contract
  * `core.project.get_project_root` reconciled with `shared_state.get_project_root`
  * a DAIC mode-switch tool round-trip

CRITICAL — two things the retired file got wrong, fixed here:
  1. Modules are imported as `core.config` / `core.project` / `tools.*` after
     inserting `plugin/mcp` on sys.path — the SAME identity the running server
     uses (`plugin/mcp/server.py`, `core/config.py`). Importing them as
     `plugin.mcp.core.*` would create a DUAL module identity whose separate
     `_project_root` / `_config` caches desync (exactly the bug that produced the
     retired file's 8 standalone failures).
  2. Nothing here touches the real `.claude/state`. Reads go through a tmp
     `CLAUDE_PROJECT_DIR`; the DAIC write test redirects shared_state's state-file
     CONSTANTS to a tmp dir and asserts the real repo state is untouched.

Run: python3 -m pytest test/test_mcp_core.py -v
"""
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MCP = REPO / "plugin" / "mcp"
HOOKS = REPO / "plugin" / "hooks"
for p in (str(MCP), str(HOOKS)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Runtime module identity: `core.*` (NOT `plugin.mcp.core.*`).
import core.config as config_module  # noqa: E402
import core.project as project_module  # noqa: E402
import shared_state  # noqa: E402


class MockMCP:
    """Captures @mcp.tool()-decorated functions by name (mirrors FastMCP's
    register surface for the tool modules)."""

    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorator(func):
            self.tools[func.__name__] = func
            return func
        return decorator

    def get_tool(self, name):
        return self.tools.get(name)


def _reset_core_caches():
    config_module._config = None
    config_module._provider = None
    config_module._config_mtime = None
    project_module._project_root = None


def _make_project(tmp_path, config):
    tm = tmp_path / "team-management"
    tm.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".claude" / "state").mkdir(parents=True, exist_ok=True)
    (tm / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    return tmp_path


@pytest.fixture
def mcp_project(tmp_path, monkeypatch):
    """A tmp project with CLAUDE_PROJECT_DIR pointed at it and both module caches
    reset before AND after — so no test leaks a cached root/config/provider."""
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    _reset_core_caches()
    yield tmp_path
    _reset_core_caches()


def _config_for(provider):
    cfg = {"issue_tracking": {"provider": provider if provider != "disabled" else "disabled"}}
    if provider == "gitlab":
        cfg["gitlab"] = {"enabled": True, "api_token": "t", "base_url": "https://gl.example.com",
                         "project_path": "x/y"}
    elif provider == "jira":
        cfg["jira"] = {"enabled": True, "api_token": "t", "base_url": "https://jira.example.com",
                       "project_key": "TEST"}
    elif provider == "github":
        cfg["github"] = {"enabled": True, "api_token": "t", "base_url": "https://api.github.com",
                         "repository": "o/r"}
    return cfg


# ------------------------------------------------------------- provider detection

@pytest.mark.parametrize("provider", ["gitlab", "jira", "github"])
def test_detect_provider_enabled(mcp_project, provider):
    _make_project(mcp_project, _config_for(provider))
    _reset_core_caches()
    assert config_module.detect_provider() == provider


def test_detect_provider_disabled(mcp_project):
    _make_project(mcp_project, _config_for("disabled"))
    _reset_core_caches()
    assert config_module.detect_provider() is None


def test_load_config(mcp_project):
    _make_project(mcp_project, _config_for("gitlab"))
    _reset_core_caches()
    cfg = config_module.load_config()
    assert cfg["gitlab"]["enabled"] is True
    assert cfg["issue_tracking"]["provider"] == "gitlab"


def test_load_config_missing_file_is_empty(mcp_project):
    # No team-management/config.json -> empty dict, never raises.
    _reset_core_caches()
    assert config_module.load_config() == {}


# ---------------------------------------------- cache poisoning regression (m-fix-mcp-config-cache-poisoning)

def test_load_config_picks_up_file_created_after_first_load(mcp_project):
    """Fresh-install order: a provider tool touches config BEFORE config.json
    exists, then the user runs /team-management:config. The empty result must NOT
    be cached for the process lifetime — the created file is picked up on the next
    load WITHOUT a manual cache reset (a full Claude Code restart in production)."""
    # First load, no file yet -> the poisoning scenario (caches {}).
    assert config_module.load_config() == {}
    # The file appears (user configures the project).
    _make_project(mcp_project, _config_for("gitlab"))
    # No _reset_core_caches() here — the fix must un-poison itself.
    cfg = config_module.load_config()
    assert cfg.get("gitlab", {}).get("enabled") is True
    assert cfg["issue_tracking"]["provider"] == "gitlab"


def test_load_config_picks_up_file_deleted_after_first_load(mcp_project):
    """The other existence direction (codex): a config.json cached at first load
    and then deleted must NOT keep returning the stale cached content."""
    _make_project(mcp_project, _config_for("gitlab"))
    assert config_module.load_config()["gitlab"]["enabled"] is True  # caches real mtime
    (mcp_project / "team-management" / "config.json").unlink()
    # No _reset_core_caches() — the deletion must be observed on the next load.
    assert config_module.load_config() == {}


def test_load_config_recovers_from_malformed_then_fixed(mcp_project):
    """The exception cache-path must also track the sentinel: a malformed
    config.json (-> {}) that is later fixed is picked up without a restart.
    os.utime forces a distinguishable mtime so the invalidation is deterministic
    on coarse-resolution filesystems (a same-tick rewrite would otherwise miss)."""
    tm = mcp_project / "team-management"
    tm.mkdir(parents=True, exist_ok=True)
    (mcp_project / ".claude" / "state").mkdir(parents=True, exist_ok=True)
    cfgfile = tm / "config.json"
    cfgfile.write_text("{ not valid json", encoding="utf-8")
    assert config_module.load_config() == {}  # exception path -> {}
    # Fix the file and bump its mtime clearly past the malformed one's.
    cfgfile.write_text(json.dumps(_config_for("gitlab")), encoding="utf-8")
    st = cfgfile.stat()
    os.utime(cfgfile, (st.st_atime + 10, st.st_mtime + 10))
    cfg = config_module.load_config()
    assert cfg.get("gitlab", {}).get("enabled") is True


# ------------------------------------------------------ disabled-provider contract

def test_issue_status_disabled(mcp_project):
    _make_project(mcp_project, _config_for("disabled"))
    _reset_core_caches()
    from tools import issue_tracking
    mock = MockMCP()
    issue_tracking.register_tools(mock)
    result = mock.get_tool("issue_status")()
    assert result["success"] is False
    assert "No issue tracking provider" in result.get("error", "")


# ------------------------------------------- get_project_root reconciliation (SC#4)

def test_project_root_matches_shared_state(mcp_project):
    """core.project.get_project_root and shared_state.get_project_root must agree
    for the env-set (CLAUDE_PROJECT_DIR) case — the documented common contract."""
    _reset_core_caches()
    assert project_module.get_project_root() == shared_state.get_project_root()
    assert project_module.get_project_root() == mcp_project


def test_project_root_no_sessions_marker():
    """The pip-era 'sessions' marker must be gone from the fallback marker list
    (check the marker-list line specifically, not prose in the docstring)."""
    src = (MCP / "core" / "project.py").read_text(encoding="utf-8")
    marker_lines = [ln for ln in src.splitlines() if "for marker in [" in ln]
    assert marker_lines, "marker-list line not found in project.py"
    assert all("sessions" not in ln for ln in marker_lines), marker_lines


# ------------------------------------------------------ DAIC tool round-trip (safe)

def test_daic_switch_writes_to_redirected_state_only(tmp_path, monkeypatch):
    """A DAIC mode-switch tool round-trip that CANNOT touch the live repo state:
    shared_state's state-file constants are redirected to tmp. Asserts the tmp
    daic-mode.json is written AND the real repo daic-mode.json is untouched."""
    state = tmp_path / ".claude" / "state"
    state.mkdir(parents=True)
    monkeypatch.setattr(shared_state, "STATE_DIR", state)
    monkeypatch.setattr(shared_state, "DAIC_STATE_FILE", state / "daic-mode.json")
    monkeypatch.setattr(shared_state, "TASK_STATE_LOCK_FILE", state / "current_task.lock")

    real_daic = REPO / ".claude" / "state" / "daic-mode.json"
    real_before = real_daic.read_bytes() if real_daic.exists() else None

    from tools import daic
    mock = MockMCP()
    daic.register_tools(mock)
    result = mock.get_tool("daic_mode_switch_documentation")()

    assert result["success"] is True
    assert result["current_mode"] == "documentation"
    written = json.loads((state / "daic-mode.json").read_text(encoding="utf-8"))
    assert written["mode"] == "documentation"

    # The live repo state must be byte-for-byte unchanged.
    real_after = real_daic.read_bytes() if real_daic.exists() else None
    assert real_after == real_before, "DAIC test must not mutate the real .claude/state"


# ------------------------------------------- protocol MCP tools end-to-end (SC#2)
# Drives protocol_start / protocol_current / protocol_advance through the SAME
# MockMCP get_tool() harness the issue tools use, against a temp project. Uses a
# minimal 2-step CUSTOM protocol so the three real tool wrappers are exercised
# WITHOUT the task protocol's git_setup_branch / create_task_file post_funcs.
#
# Isolation: shared_state freezes PROJECT_ROOT / STATE_DIR / all state-file paths
# at import time (test_mcp_core imports it at collection), so setting only
# CLAUDE_PROJECT_DIR is insufficient — the frozen constants are patched to the
# temp dir (mirror test_protocol_optimize_e2e.py:104-118) and restored after.

_SHARED_STATE_KEYS = (
    "PROJECT_ROOT", "STATE_DIR", "TASK_STATE_FILE", "DAIC_STATE_FILE",
    "PROTOCOL_LOGS_DIR", "TASK_STATE_LOCK_FILE",
)

_MINI_PROTOCOL = {
    "name": "mini",
    "description": "Minimal 2-step protocol for MCP tool tests.",
    "steps": [
        {"name": "step-one", "description": "first", "mode": "discussion",
         "pre_funcs": [], "post_funcs": [], "advance_args": [],
         "start": "Step one.", "end": "Advance when ready."},
        {"name": "step-two", "description": "second", "mode": "discussion",
         "pre_funcs": [], "post_funcs": [], "advance_args": [],
         "start": "Step two.", "end": "Done."},
    ],
}


@pytest.fixture
def protocol_project(tmp_path, monkeypatch):
    """Temp project wired for the protocol engine: CLAUDE_PROJECT_DIR + the frozen
    shared_state path constants both point at tmp; CLAUDE_PLUGIN_ROOT scrubbed so
    get_plugin_root() resolves to the repo's plugin dir (the code under test)."""
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)

    state = tmp_path / ".claude" / "state"
    (state / "protocol-logs").mkdir(parents=True)
    (state / "daic-mode.json").write_text(json.dumps({"mode": "discussion"}),
                                          encoding="utf-8")
    (state / "current_task.json").write_text(json.dumps(
        {"task": None, "branch": None, "services": [], "updated": None}),
        encoding="utf-8")
    tm = tmp_path / "team-management"
    tm.mkdir(parents=True, exist_ok=True)
    (tm / "config.json").write_text(
        json.dumps({"protocol_engine": {"enabled": True}}), encoding="utf-8")
    custom = tm / "protocol-configs" / "custom"
    custom.mkdir(parents=True)
    (custom / "mini.json").write_text(json.dumps(_MINI_PROTOCOL), encoding="utf-8")

    orig = {k: getattr(shared_state, k) for k in _SHARED_STATE_KEYS}
    shared_state.PROJECT_ROOT = tmp_path
    shared_state.STATE_DIR = state
    shared_state.TASK_STATE_FILE = state / "current_task.json"
    shared_state.DAIC_STATE_FILE = state / "daic-mode.json"
    shared_state.PROTOCOL_LOGS_DIR = state / "protocol-logs"
    shared_state.TASK_STATE_LOCK_FILE = state / "current_task.lock"
    # Reset the core.project / core.config caches too: get_hooks_path resolves the
    # provider-utils dir via get_plugin_root()'s __file__-relative dev candidate
    # (-> repo plugin/hooks) regardless, but core.project._project_root is cached
    # independently of shared_state, so clearing it keeps this fixture order-
    # independent w.r.t. tests that populated that cache (codex hardening).
    _reset_core_caches()
    try:
        yield tmp_path
    finally:
        for k, v in orig.items():
            setattr(shared_state, k, v)
        _reset_core_caches()


def _protocol_tools():
    from tools import protocol
    mock = MockMCP()
    protocol.register_tools(mock)
    return mock


def test_protocol_start_current_advance_roundtrip(protocol_project):
    """protocol_start -> protocol_current -> protocol_advance drive the 2-step
    custom protocol end-to-end; temp current_task.json records the transition and
    the real repo .claude/state is never touched."""
    real_task = REPO / ".claude" / "state" / "current_task.json"
    real_before = real_task.read_bytes() if real_task.exists() else None

    mcp = _protocol_tools()

    started = mcp.get_tool("protocol_start")("mini")
    assert started["success"] is True, started
    assert started["step"]["name"] == "step-one"
    assert started["step"]["index"] == 0
    assert started["step"]["total_steps"] == 2

    current = mcp.get_tool("protocol_current")()
    assert current["success"] is True
    assert current["active"] is True
    assert current["step"]["name"] == "step-one"

    advanced = mcp.get_tool("protocol_advance")("step one done")
    assert advanced["success"] is True, advanced
    assert advanced["previous_step"]["name"] == "step-one"
    assert advanced["step"]["name"] == "step-two"
    assert advanced["step"]["index"] == 1

    # Engine state file written under the temp project.
    written = json.loads((protocol_project / ".claude" / "state" / "current_task.json")
                         .read_text(encoding="utf-8"))
    assert written["protocol"]["name"] == "mini"
    assert written["protocol"]["step_name"] == "step-two"

    # The live repo state must be byte-for-byte unchanged.
    real_after = real_task.read_bytes() if real_task.exists() else None
    assert real_after == real_before, "protocol test must not mutate the real .claude/state"


def test_protocol_start_real_task_protocol(protocol_project):
    """The real `task` protocol loads through the tool wrapper and reports its
    investigation step (exercises the real-config load path; no advance, so the
    create_task_file / git post_funcs — covered elsewhere — are not triggered)."""
    mcp = _protocol_tools()
    started = mcp.get_tool("protocol_start")("task")
    assert started["success"] is True, started
    assert started["step"]["name"] == "investigation"


def test_protocol_start_engine_disabled(protocol_project):
    """protocol_engine.enabled=false -> the tool wrapper's _check_enabled gate
    returns the disabled error instead of starting."""
    (protocol_project / "team-management" / "config.json").write_text(
        json.dumps({"protocol_engine": {"enabled": False}}), encoding="utf-8")
    mcp = _protocol_tools()
    result = mcp.get_tool("protocol_start")("mini")
    assert result["success"] is False
    assert "disabled" in result.get("error", "").lower()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
