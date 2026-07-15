#!/usr/bin/env python3
"""Plugin-runtime probe (m-plugin-verification, Layer A).

NOT a pytest module (leading underscore → not collected). `test_plugin_runtime_e2e.py`
runs this as a SUBPROCESS under a controlled plugin environment:
  - CLAUDE_PLUGIN_ROOT = <repo>/plugin   (the read-only plugin install)
  - CLAUDE_PROJECT_DIR = <tmp project>   (the user's project)
  - cwd = some dir OUTSIDE the repo
  - NO PYTHONPATH, no stray CLAUDE_* vars

It reproduces `plugin/mcp/server.py`'s own path bootstrap (insert
`${CLAUDE_PLUGIN_ROOT}/mcp` on sys.path, then `setup_provider_imports()` which adds
`${CLAUDE_PLUGIN_ROOT}/hooks`) so the resolution it exercises is the plugin's, NOT an
injected import path — otherwise the test would paper over a real path bug (codex
warning, task D2).

It prints ONE JSON object to stdout with everything the harness asserts:
  - registered_tools: every @mcp.tool name from all 8 tool modules (SC3)
  - protocol_names / protocol_sources / protocol_warnings (SC1/SC2/SC3)
  - *_ref_resolved: @sub-protocols / @knowledge resolve from PLUGIN_ROOT (SC1/SC2)
  - codex template plugin-tier match + custom-sentinel presence (SC4)
  - resolved roots + cwd (diagnostics)

Assertions live in the pytest file; this script only gathers facts.
"""

import json
import os
import sys
from pathlib import Path

CUSTOM_SENTINEL = "CUSTOM_OVERRIDE_SENTINEL_XYZ"


def main():
    out = {"errors": []}
    plugin_root = Path(os.environ["CLAUDE_PLUGIN_ROOT"]).resolve()
    project_dir = Path(os.environ["CLAUDE_PROJECT_DIR"]).resolve()

    # --- reproduce server.py's path bootstrap (no PYTHONPATH injection) ---
    sys.path.insert(0, str(plugin_root / "mcp"))
    from core.project import setup_provider_imports
    setup_provider_imports()  # adds ${CLAUDE_PLUGIN_ROOT}/hooks to sys.path

    import shared_state
    out["resolved_plugin_root"] = str(shared_state.get_plugin_root())
    out["resolved_project_root"] = str(shared_state.get_project_root())
    out["cwd"] = os.getcwd()

    # --- SC3: register all 8 tool modules on a MockMCP ---
    class MockMCP:
        def __init__(self):
            self.tools = {}

        def tool(self):
            def deco(fn):
                self.tools[fn.__name__] = fn
                return fn
            return deco

    mcp = MockMCP()
    from tools import (daic, release, git_operations, issue_tracking,
                       code_review, protocol, notifications, config)
    for mod in (daic, release, git_operations, issue_tracking,
                code_review, protocol, notifications, config):
        mod.register_tools(mcp)
    out["registered_tools"] = sorted(mcp.tools.keys())

    # --- SC1/SC2/SC3: protocols via ProtocolEngine.list_protocols() ---
    import protocol_engine
    engine = protocol_engine.ProtocolEngine(project_dir)
    lp = engine.list_protocols()
    out["protocol_names"] = sorted(p["name"] for p in lp.get("protocols", []))
    out["protocol_sources"] = {p["name"]: p.get("source") for p in lp.get("protocols", [])}
    out["protocol_warnings"] = lp.get("warnings", [])

    # --- SC1/SC2: @-ref resolution from PLUGIN_ROOT ---
    inv = shared_state.resolve_protocol_start_text("@sub-protocols/task-investigation.md", "task")
    out["investigation_ref_resolved"] = "SCOPE OF THIS STEP" in inv
    out["investigation_ref_literal_remaining"] = "@sub-protocols/task-investigation.md" in inv
    kn = shared_state.resolve_protocol_start_text("@knowledge/tdd-discipline.md", "task")
    out["knowledge_ref_resolved"] = "RED-GREEN-REFACTOR" in kn

    # --- SC4: provider template resolution (plugin tier; custom override) ---
    from ai_providers import _DefaultEmptyDict
    tmpl = engine._load_provider_template("investigation", "codex", {})
    out["template_contains_custom_sentinel"] = CUSTOM_SENTINEL in tmpl
    plugin_tmpl_file = plugin_root / "protocol-configs" / "providers" / "codex-investigation.md"
    if plugin_tmpl_file.exists():
        expected = plugin_tmpl_file.read_text(encoding="utf-8").format_map(_DefaultEmptyDict({}))
        out["plugin_template_matches"] = (tmpl == expected)
    else:
        out["plugin_template_matches"] = False
        out["errors"].append("plugin codex-investigation.md missing")

    print(json.dumps(out))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # surface the failure to the harness rather than a bare traceback-only exit
        import traceback
        print(json.dumps({"fatal": f"{type(exc).__name__}: {exc}",
                          "traceback": traceback.format_exc()}))
        sys.exit(1)
