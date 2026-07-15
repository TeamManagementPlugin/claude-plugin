"""Drift guard for h-fix-mcp-manager-split-and-notebookedit (Defect 2).

`plugin/hooks/hooks.json` registers `sessions-enforce.py` behind a PreToolUse
`matcher` (a `|`-joined tool-name alternation). The hook only ever runs for a
tool whose name is in that matcher. `sessions-enforce.py` special-cases a set of
tools by comparing `tool_name` against string literals (and against
`DEFAULT_CONFIG["blocked_tools"]`). If a special-cased tool is missing from the
matcher, the hook never fires for it and DAIC/branch/protected-path enforcement
silently does not exist for that tool — which is exactly how `NotebookEdit`
slipped through.

This test DERIVES the special-cased tool set from `sessions-enforce.py`'s source
(via AST) and asserts every one of them is present in the matcher, so the next
tool added to a `tool_name in (...)` gate cannot silently miss the matcher.
"""
import ast
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HOOKS_DIR = _REPO / "plugin" / "hooks"
_HOOKS_JSON = _HOOKS_DIR / "hooks.json"
_SESSIONS_ENFORCE = _HOOKS_DIR / "sessions-enforce.py"


def _sessions_enforce_matcher() -> set:
    """The `|`-split tool set from the PreToolUse matcher gating sessions-enforce.py."""
    data = json.loads(_HOOKS_JSON.read_text(encoding="utf-8"))
    pre_tool_use = data["hooks"]["PreToolUse"]
    for entry in pre_tool_use:
        commands = " ".join(h.get("command", "") for h in entry.get("hooks", []))
        if "sessions-enforce.py" in commands:
            matcher = entry.get("matcher", "")
            return {tok.strip() for tok in matcher.split("|") if tok.strip()}
    raise AssertionError("No PreToolUse entry wires sessions-enforce.py in hooks.json")


def _str_literals(node) -> set:
    """String constants directly in `node` or inside a list/tuple/set literal."""
    out = set()
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        out.add(node.value)
    elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        for elt in node.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                out.add(elt.value)
    return out


def _tools_special_cased_by_source() -> set:
    """Every tool name sessions-enforce.py compares `tool_name` against, plus the
    DEFAULT_CONFIG['blocked_tools'] defaults (used via `tool_name in ...`)."""
    tree = ast.parse(_SESSIONS_ENFORCE.read_text(encoding="utf-8"))
    tools = set()

    for node in ast.walk(tree):
        # (a) any comparison in which `tool_name` participates
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            has_tool_name = any(
                isinstance(op, ast.Name) and op.id == "tool_name" for op in operands
            )
            if has_tool_name:
                for op in operands:
                    if isinstance(op, ast.Name) and op.id == "tool_name":
                        continue
                    tools |= _str_literals(op)

        # (b) DEFAULT_CONFIG = {... "blocked_tools": [...] ...}
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "blocked_tools":
                    tools |= _str_literals(value)

    return tools


def test_matcher_covers_every_special_cased_tool():
    matcher = _sessions_enforce_matcher()
    special_cased = _tools_special_cased_by_source()

    # Sanity: both derivations found something (guards against silent parse breakage).
    assert special_cased, "AST derived no tool_name comparisons — parser drift?"
    assert matcher, "sessions-enforce matcher parsed empty"

    missing = special_cased - matcher
    assert not missing, (
        "sessions-enforce.py special-cases these tools but the hooks.json "
        f"PreToolUse matcher does not cover them: {sorted(missing)}. "
        f"Matcher={sorted(matcher)}, special-cased={sorted(special_cased)}"
    )


def test_notebookedit_present():
    """Explicit regression anchor for Defect 2."""
    matcher = _sessions_enforce_matcher()
    assert "NotebookEdit" in matcher
    # And it is genuinely special-cased (guards against a vacuous pass).
    assert "NotebookEdit" in _tools_special_cased_by_source()
