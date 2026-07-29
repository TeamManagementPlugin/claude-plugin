"""Drift-guard: protocol step end conditions are checkbox lists.

Every non-empty ``steps[].end`` string in the 6 system protocol JSONs must be
formatted as a markdown checkbox list (``- [ ]`` items). The end text is
injected verbatim into stderr by post-tool-use.py on the first tool call after
a protocol-state change and every 10th call thereafter — a scannable checklist
reads better than a prose wall at that frequency, and this test keeps future
edits from silently regressing to prose.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROTOCOL_CONFIGS = REPO_ROOT / "plugin" / "protocol-configs"

PROTOCOL_FILES = (
    "task.json",
    "brainstorm.json",
    "research.json",
    "refactoring.json",
    "optimize.json",
    "optimize-unattended.json",
)

CHECKBOX_MARKER = "- [ ]"


def _load_steps(filename: str) -> list[dict]:
    cfg = json.loads((PROTOCOL_CONFIGS / filename).read_text(encoding="utf-8"))
    return cfg["steps"]


def _has_checkbox_line(text: str) -> bool:
    """True when at least one line IS a checkbox item (not a mere inline
    mention of the marker). Intro lines ("Advance when:") and informational
    outro lines are allowed by design — the contract is 'checkbox list with
    optional prose framing', not 'every line is a checkbox'."""
    return any(line.lstrip().startswith(CHECKBOX_MARKER) for line in text.splitlines())


@pytest.mark.parametrize("filename", PROTOCOL_FILES)
def test_all_end_conditions_are_checkbox_lists(filename: str) -> None:
    steps = _load_steps(filename)
    assert steps, f"{filename} has no steps"
    offenders = [
        step["name"]
        for step in steps
        if step.get("end", "").strip() and not _has_checkbox_line(step["end"])
    ]
    assert not offenders, (
        f"{filename}: steps with prose (non-checklist) end conditions: {offenders}. "
        f"Format every end condition as a markdown checkbox list ('{CHECKBOX_MARKER} ...')."
    )


@pytest.mark.parametrize("filename", PROTOCOL_FILES)
def test_end_conditions_are_present(filename: str) -> None:
    """Companion guard: no step silently drops its end condition to dodge the
    checklist rule — every step keeps a non-empty end text."""
    steps = _load_steps(filename)
    empty = [step["name"] for step in steps if not step.get("end", "").strip()]
    assert not empty, f"{filename}: steps with missing/empty end conditions: {empty}"
