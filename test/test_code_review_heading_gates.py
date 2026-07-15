#!/usr/bin/env python3
"""Drift-guard: code-review work-log headings stay parseable by the completion gates.

Two engine gates parse the `# Code Review:` work-log block, and they are NOT
symmetric about the emoji:

  - Critical gate (`protocol_engine._func_validate_no_critical_issues_in_worklog`)
    parses `^##\\s*(?:🔴\\s*)?Critical Issues\\s*\\((\\d+)\\)` — the 🔴 is OPTIONAL.
  - Warnings gate (`protocol_utils.check_code_review_warnings`) parses
    `##\\s*🟡\\s*Warnings?\\s*\\((\\d+)\\)` — the 🟡 is REQUIRED. A plain
    `## Warnings (N)` is INVISIBLE to it, so warning enforcement silently passes.

Both the code-review AGENT output template (`plugin/agents/code-review.md`) and the
sub-protocol work-log template (`plugin/protocol-configs/sub-protocols/code-review.md`)
must therefore emit the EMOJI headings. This guard fails if a future edit drops an
emoji from either template (which would blind the warnings gate), or if the gate
function stops accepting the emoji heading.

Run with: python3 -m pytest test/test_code_review_heading_gates.py -v
"""

import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "plugin" / "hooks"
sys.path.insert(0, str(HOOKS))

from protocol_utils import check_code_review_warnings  # noqa: E402

AGENT_TEMPLATE = REPO / "plugin" / "agents" / "code-review.md"
SUBPROTO_TEMPLATE = REPO / "plugin" / "protocol-configs" / "sub-protocols" / "code-review.md"

# Mirror of the critical-gate regex in
# protocol_engine._func_validate_no_critical_issues_in_worklog (🔴 optional).
CRITICAL_RE = re.compile(r"^##\s*(?:🔴\s*)?Critical Issues\s*\((\d+)\)", re.MULTILINE)


# --- gate FUNCTION behaviour (digit-filled headings) ---

def test_warnings_gate_parses_emoji_heading():
    block = "# Code Review: X\n\n## 🟡 Warnings (3)\n\n### 1. a\n### 2. b\n### 3. c\n"
    res = check_code_review_warnings(block)
    assert res["has_warnings"] is True
    assert res["warning_count"] == 3


def test_warnings_gate_zero_count_is_not_warnings():
    res = check_code_review_warnings("# Code Review: X\n\n## 🟡 Warnings (0)\nNone.\n")
    assert res["has_warnings"] is False


def test_warnings_gate_is_blind_to_plain_heading():
    # Documents the strict requirement that motivates the emoji: a PLAIN
    # '## Warnings (N)' is not seen by the gate. If this ever changes, the
    # emoji-in-template requirement below can be relaxed.
    res = check_code_review_warnings("# Code Review: X\n\n## Warnings (3)\n\n### 1. a\n")
    assert res["has_warnings"] is False


def test_critical_gate_regex_accepts_emoji_and_plain():
    assert CRITICAL_RE.search("## 🔴 Critical Issues (0)")
    assert CRITICAL_RE.search("## Critical Issues (2)")


# --- template DRIFT guards (both templates must emit the emoji headings) ---

@pytest.mark.parametrize("path", [AGENT_TEMPLATE, SUBPROTO_TEMPLATE], ids=lambda p: p.parent.name + "/" + p.name)
def test_template_emits_emoji_warnings_heading(path):
    text = path.read_text(encoding="utf-8")
    assert "## 🟡 Warnings (" in text, (
        f"{path.name} must emit the '## 🟡 Warnings (' heading — the warnings gate "
        "(protocol_utils.check_code_review_warnings) requires the 🟡 emoji, so dropping "
        "it silently blinds warning enforcement."
    )


@pytest.mark.parametrize("path", [AGENT_TEMPLATE, SUBPROTO_TEMPLATE], ids=lambda p: p.parent.name + "/" + p.name)
def test_template_critical_heading_is_gate_parseable(path):
    # The critical heading must keep the 🔴 emoji (task convention) AND carry a
    # concrete digit count the gate actually parses. A literal '(N)' fails CLOSED
    # (the gate reports "missing Critical Issues subsection" and BLOCKS completion),
    # so the canonical template must ship a real digit, e.g. '(0)' for "none found".
    text = path.read_text(encoding="utf-8")
    assert "## 🔴 Critical Issues (" in text, (
        f"{path.name} must keep the 🔴 emoji on the Critical Issues heading (task convention)."
    )
    assert CRITICAL_RE.search(text), (
        f"{path.name} Critical Issues heading must carry a gate-parseable digit count "
        "(e.g. '## 🔴 Critical Issues (0)'); a literal '(N)' fails closed and blocks completion."
    )
