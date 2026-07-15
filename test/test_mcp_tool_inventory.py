"""Drift-guard for the MCP tool inventory.

The single canonical list of registered MCP tools is ``EXPECTED_TOOLS`` below.
This test statically parses every ``plugin/mcp/tools/*.py`` module
(excluding ``__init__.py``) for functions decorated with ``@mcp.tool`` /
``@mcp.tool()`` and asserts the parsed name-set matches ``EXPECTED_TOOLS``
exactly -- catching both COUNT drift (a tool added/removed) and NAME drift
(a tool renamed).

It deliberately does NOT import ``mcp`` or ``server.py``: the decorators
are applied inside each module's ``register_tools(mcp)`` function, so a static
AST walk is both sufficient and dependency-free (runs on any platform without
the MCP runtime installed).

Two invariants make the static walk faithful to runtime and are themselves
asserted here, so they cannot silently rot:

* ``test_no_name_override`` -- no ``@mcp.tool`` decorator overrides the tool
  name, either positionally (``@mcp.tool("x")``) or via a ``name=`` kwarg, so
  the tool name == the function name. If someone adds an override later, this
  test fails loudly instead of the parser silently reporting the wrong name.
  (Benign metadata kwargs like ``description=`` are allowed.)
* ``test_server_registers_exactly_the_tool_modules`` -- ``server.py`` actually
  calls ``register_tools(mcp)`` on exactly the set of modules that contain
  tools. A ``tools/*.py``-only scan would still pass if a future edit dropped a
  ``register_tools`` call; this assertion catches that.

If you add/remove/rename an ``@mcp.tool``, this test fails. To fix it:
  1. Update ``EXPECTED_TOOLS`` below.
  2. Update the canonical count in ``plugin/mcp/CLAUDE.md``
     (``## MCP Tools (N total)``) -- also asserted here.
  3. Update the human-readable counts in ``CLAUDE.md`` and
     ``plugin/CLAUDE.md`` (prose mirrors, not machine-checked).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "plugin" / "mcp" / "tools"
SERVER_PY = REPO_ROOT / "plugin" / "mcp" / "server.py"
MCP_CLAUDE_MD = REPO_ROOT / "plugin" / "mcp" / "CLAUDE.md"
ROOT_CLAUDE_MD = REPO_ROOT / "CLAUDE.md"

# Single canonical inventory. Grouped by module for human readability; the test
# only cares about the flat set. Total = 42.
EXPECTED_TOOLS = frozenset({
    # code_review.py (4)
    "merge_request_comment",
    "pull_request_comment",
    "code_review",
    "fetch_mr_review",
    # daic.py (3)
    "daic_mode_switch_discussion",
    "daic_mode_switch_implementation",
    "daic_mode_switch_documentation",
    # git_operations.py (4)
    "git_commit",
    "git_push",
    "merge_request_create",
    "merge_request_update",
    # issue_tracking.py (14)
    "issue_status",
    "config_issue_tracking_status",
    "config_code_review_enforcement",
    "issue_read",
    "issue_create",
    "issue_update",
    "issue_sync",
    "issue_push",
    "issue_link",
    "issue_unlink",
    "issue_comment",
    "issue_set_status",
    "issue_api",
    "issue_dependency",
    # notifications.py (3)
    "notify_user",
    "notification_status",
    "notification_discover_telegram_chats",
    # protocol.py (11)
    "protocol_list",
    "protocol_current",
    "protocol_advance",
    "protocol_goto",
    "protocol_log",
    "protocol_abort",
    "protocol_save_note",
    "protocol_available_funcs",
    "protocol_start",
    "protocol_customize",
    "protocol_check_drift",
    # release.py (1)
    "release_create",
    # config.py (2)
    "config_get",
    "config_update",
})

EXPECTED_COUNT = 42


def _tool_decorator_keywords(node: ast.AST):
    """If ``node`` is an ``@mcp.tool`` / ``@mcp.tool(...)`` decorator, return its
    list of keyword args (``[]`` for the bare/call-no-kwargs form). Return
    ``None`` when the decorator is not ``mcp.tool``."""
    target = node.func if isinstance(node, ast.Call) else node
    is_mcp_tool = (
        isinstance(target, ast.Attribute)
        and target.attr == "tool"
        and isinstance(target.value, ast.Name)
        and target.value.id == "mcp"
    )
    if not is_mcp_tool:
        return None
    return node.keywords if isinstance(node, ast.Call) else []


def _tool_module_files():
    return sorted(p for p in TOOLS_DIR.glob("*.py") if p.name != "__init__.py")


def _registered_tools():
    """Map ``{tool_name: source_path}`` for every ``@mcp.tool`` function found by
    a full AST walk of each tool module (decorators are nested inside
    ``register_tools(mcp)``)."""
    found = {}
    for path in _tool_module_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if _tool_decorator_keywords(dec) is not None:
                    found[node.name] = path
                    break
    return found


def _name_override_violations():
    """Return ``[(path, lineno)]`` for any ``@mcp.tool(...)`` decorator that
    overrides the tool name -- a positional first arg (``@mcp.tool("x")``) or a
    ``name=`` keyword. Benign metadata kwargs (e.g. ``description=``) are NOT
    flagged. Any hit means the tool name no longer equals the function name, so
    the static parser (which reads the function name) -- and thus
    ``EXPECTED_TOOLS`` -- would silently diverge from the registered name."""
    violations = []
    for path in _tool_module_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            for dec in getattr(node, "decorator_list", []):
                if _tool_decorator_keywords(dec) is None:
                    continue  # not an @mcp.tool decorator
                if isinstance(dec, ast.Call):
                    has_name_kw = any(kw.arg == "name" for kw in dec.keywords)
                    if dec.args or has_name_kw:
                        violations.append((path.name, dec.lineno))
    return violations


def _module_has_tool(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(_tool_decorator_keywords(d) is not None for d in node.decorator_list):
                return True
    return False


def _server_registered_modules():
    """Module names ``X`` for every ``X.register_tools(mcp)`` call in server.py."""
    tree = ast.parse(SERVER_PY.read_text(encoding="utf-8"), filename=str(SERVER_PY))
    modules = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "register_tools"
            and isinstance(node.func.value, ast.Name)
        ):
            modules.add(node.func.value.id)
    return modules


def test_registered_set_matches_expected():
    registered = set(_registered_tools())
    extra = registered - EXPECTED_TOOLS
    missing = EXPECTED_TOOLS - registered
    assert registered == EXPECTED_TOOLS, (
        "MCP tool inventory drift.\n"
        f"  In code but NOT in EXPECTED_TOOLS (add to docs + EXPECTED_TOOLS): {sorted(extra)}\n"
        f"  In EXPECTED_TOOLS but NOT in code (remove from docs + EXPECTED_TOOLS): {sorted(missing)}\n"
        "Then update the count in plugin/mcp/CLAUDE.md (## MCP Tools (N total)) "
        "and the prose counts in CLAUDE.md / plugin/CLAUDE.md."
    )


def test_registered_count_is_expected():
    assert len(_registered_tools()) == EXPECTED_COUNT == len(EXPECTED_TOOLS)


def test_canonical_doc_count_matches():
    text = MCP_CLAUDE_MD.read_text(encoding="utf-8")
    m = re.search(r"##\s*MCP Tools\s*\((\d+)\s*total\)", text)
    assert m, (
        "Canonical inventory header '## MCP Tools (N total)' not found in "
        f"{MCP_CLAUDE_MD}. Keep that exact header so this guard can read it."
    )
    assert int(m.group(1)) == EXPECTED_COUNT, (
        f"plugin/mcp/CLAUDE.md says {m.group(1)} tools; live inventory is "
        f"{EXPECTED_COUNT}."
    )


def test_no_name_override():
    violations = _name_override_violations()
    assert not violations, (
        "An @mcp.tool decorator overrides the tool name (positional arg or "
        "name= kwarg). This test assumes tool name == function name; update "
        "_registered_tools to resolve the override before EXPECTED_TOOLS can "
        f"be trusted again. Offending decorators: {violations}"
    )


def test_server_registers_exactly_the_tool_modules():
    registered = _server_registered_modules()
    with_tools = {p.stem for p in _tool_module_files() if _module_has_tool(p)}
    assert registered == with_tools, (
        "server.py register_tools(mcp) calls do not match the set of tool "
        "modules.\n"
        f"  Registered in server.py but no tools found: {sorted(registered - with_tools)}\n"
        f"  Has tools but NOT registered in server.py: {sorted(with_tools - registered)}"
    )


def _module_tool_count(module_stem: str) -> int:
    """Count of ``@mcp.tool`` functions whose source module is ``<module_stem>.py``."""
    return sum(1 for path in _registered_tools().values() if path.stem == module_stem)


def test_root_claude_md_notifications_tree_line():
    """The MCP modular-structure tree in the root ``CLAUDE.md`` annotates
    ``notifications.py`` with its live per-module tool count. This guards the
    per-module counts (whose sum must equal ``EXPECTED_COUNT``) from re-drifting --
    the failure that motivated m-docs-audit-drift-sweep, where the tree still said
    ``# 2 notification tools`` after a third tool was registered. The expected
    value is DERIVED from the registered set, so no hardcoded count is introduced."""
    live = _module_tool_count("notifications")
    text = ROOT_CLAUDE_MD.read_text(encoding="utf-8")
    m = re.search(
        r"^\s*notifications\.py\s+#\s*(\d+)\s+notification tools", text, re.MULTILINE
    )
    assert m, (
        "Root CLAUDE.md MCP modular-structure tree line "
        "'notifications.py   # N notification tools' not found -- keep that line "
        "so this guard can verify the per-module count."
    )
    assert int(m.group(1)) == live, (
        f"Root CLAUDE.md says {m.group(1)} notification tools; the live registered "
        f"count is {live}. Update the tree line in CLAUDE.md."
    )
