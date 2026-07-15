#!/usr/bin/env python3
"""Layer A plugin-runtime verification (m-plugin-verification, commit 2).

Runs `_plugin_runtime_probe.py` as a SUBPROCESS under a controlled plugin
environment (CLAUDE_PLUGIN_ROOT set, CLAUDE_PROJECT_DIR = tmp project, cwd OUTSIDE
the repo, NO PYTHONPATH, no stray CLAUDE_* vars) and asserts the facts it prints.

A subprocess (not in-process) is deliberate:
  - it exercises the plugin's OWN path bootstrap, not an injected import path
    (codex paper-over warning, task D2);
  - it gives a fresh interpreter each run, so the `mcp/core/project._project_root`
    module cache cannot leak between scenarios (codex hidden-coupling note).

What this proves (Layer A) and what it does NOT:
  - SC3: all 8 tool modules register their tools under plugin path resolution, and
    the set equals the canonical inventory — but NOT the live FastMCP stdio
    handshake or Claude's plugin loader (that is Layer B / the report).
  - SC4: provider templates resolve from PLUGIN_ROOT, custom override from
    PROJECT_DIR wins.
  - SC1/SC2: all 6 protocol configs load from PLUGIN_ROOT and @sub-protocols /
    @knowledge start-text refs resolve from PLUGIN_ROOT — but NOT a full in-session
    protocol round-trip (Layer B / C).

The 42-tool oracle is imported from `test_mcp_tool_inventory.EXPECTED_TOOLS` — a
SINGLE canonical list, not a second copy (codex simpler-alternative).

Run with: python3 -m pytest test/test_plugin_runtime_e2e.py -v
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO / "plugin"
PROBE = REPO / "test" / "_plugin_runtime_probe.py"

# Single source of truth for the expected tool inventory (no second 42-list).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_mcp_tool_inventory import EXPECTED_TOOLS  # noqa: E402

EXPECTED_PROTOCOLS = {
    "task", "brainstorm", "research", "refactoring", "optimize", "optimize-unattended",
}
CUSTOM_SENTINEL = "CUSTOM_OVERRIDE_SENTINEL_XYZ"


def _run_probe(tmp_path, custom_template=False):
    """Run the probe under a minimal plugin environment; return parsed JSON.

    custom_template=True writes a sentinel codex-investigation.md into the tmp
    project's custom/providers/ so SC4's override precedence can be asserted.
    """
    proj = tmp_path / "proj"
    (proj / "team-management").mkdir(parents=True)
    if custom_template:
        cust = proj / "team-management" / "protocol-configs" / "custom" / "providers"
        cust.mkdir(parents=True)
        (cust / "codex-investigation.md").write_text(
            f"# custom {CUSTOM_SENTINEL}\n", encoding="utf-8"
        )

    workdir = tmp_path / "elsewhere"  # cwd deliberately outside the repo
    workdir.mkdir()

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
        "CLAUDE_PROJECT_DIR": str(proj),
        # deliberately NO PYTHONPATH / CLAUDE_PLUGIN_DATA / other CLAUDE_*.
    }
    r = subprocess.run(
        [sys.executable, str(PROBE)],
        capture_output=True, text=True, env=env, cwd=str(workdir), timeout=120,
    )
    assert r.returncode == 0, f"probe failed (rc={r.returncode})\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"
    # The probe prints exactly one JSON object on the last non-empty stdout line.
    line = [ln for ln in r.stdout.splitlines() if ln.strip()][-1]
    data = json.loads(line)
    assert "fatal" not in data, data
    return data, r.stderr


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    return _run_probe(tmp_path_factory.mktemp("plugin_rt"))


# --------------------------------------------------------------------------
# SC3 — MCP tool registration + protocol count
# --------------------------------------------------------------------------

def test_all_tools_register_under_plugin_env(probe):
    data, _ = probe
    registered = set(data["registered_tools"])
    missing = EXPECTED_TOOLS - registered
    extra = registered - EXPECTED_TOOLS
    assert not missing, f"tools NOT registered under plugin env: {sorted(missing)}"
    assert not extra, f"unexpected tools registered: {sorted(extra)}"
    assert len(registered) == len(EXPECTED_TOOLS) == 42


def test_six_protocols_from_plugin_root(probe):
    data, _ = probe
    assert set(data["protocol_names"]) == EXPECTED_PROTOCOLS
    assert len(data["protocol_names"]) == 6
    # Every protocol resolved from the plugin install tier (not legacy/custom).
    assert all(src == "system" for src in data["protocol_sources"].values()), data["protocol_sources"]
    assert data["protocol_warnings"] == [], data["protocol_warnings"]


# --------------------------------------------------------------------------
# SC1 / SC2 — @-ref start-text resolution from PLUGIN_ROOT
# --------------------------------------------------------------------------

def test_subprotocol_ref_resolves_from_plugin_root(probe):
    data, _ = probe
    assert data["investigation_ref_resolved"] is True
    assert data["investigation_ref_literal_remaining"] is False


def test_knowledge_ref_resolves_from_plugin_root(probe):
    data, _ = probe
    assert data["knowledge_ref_resolved"] is True


# --------------------------------------------------------------------------
# SC4 — provider template resolution + custom override
# --------------------------------------------------------------------------

def test_provider_template_from_plugin_root(probe):
    data, _ = probe
    assert data["plugin_template_matches"] is True
    assert data["template_contains_custom_sentinel"] is False


def test_custom_provider_template_overrides(tmp_path):
    data, _ = _run_probe(tmp_path, custom_template=True)
    assert data["template_contains_custom_sentinel"] is True


# --------------------------------------------------------------------------
# Diagnostics — roots resolved env-first despite divergent cwd
# --------------------------------------------------------------------------

def test_roots_resolve_env_first(probe):
    data, _ = probe
    assert data["resolved_plugin_root"] == str(PLUGIN_ROOT)
    assert data["resolved_project_root"].endswith("proj")
    assert data["cwd"] != str(REPO)  # cwd is genuinely outside the repo
