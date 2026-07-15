"""Drift-guard for the optimize protocol pair.

`optimize.json` and `optimize-unattended.json` share six step blocks verbatim:
`setup`, `metric-script`, `baseline`, `synthesis`, `code-review`, `completion`.
Only `experimentation` differs (batch checkpoint vs autonomous loop).

This test fails when an intentional or accidental change drifts the shared
blocks apart. If you need to modify a shared step block, apply the change
to BOTH files in the same commit.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest


SHARED_STEPS = (
    "setup",
    "metric-script",
    "baseline",
    "synthesis",
    "code-review",
    "completion",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
OPTIMIZE_JSON = REPO_ROOT / "plugin" / "protocol-configs" / "optimize.json"
OPTIMIZE_UNATTENDED_JSON = REPO_ROOT / "plugin" / "protocol-configs" / "optimize-unattended.json"


def _load_steps_by_name(path: Path) -> dict[str, dict]:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    return {step["name"]: step for step in cfg["steps"]}


def _serialize(step: dict) -> str:
    return json.dumps(step, sort_keys=True, ensure_ascii=False)


def _assert_blocks_identical(steps_a: dict, steps_b: dict) -> None:
    for name in SHARED_STEPS:
        assert name in steps_a, f"optimize.json missing shared step '{name}'"
        assert name in steps_b, f"optimize-unattended.json missing shared step '{name}'"
        a = _serialize(steps_a[name])
        b = _serialize(steps_b[name])
        assert a == b, (
            f"DRIFT in step '{name}'.\n"
            f"optimize.json          : {a}\n"
            f"optimize-unattended.json: {b}\n"
            f"Apply the change to both files in the same commit."
        )


def test_shared_step_blocks_are_byte_identical():
    steps_a = _load_steps_by_name(OPTIMIZE_JSON)
    steps_b = _load_steps_by_name(OPTIMIZE_UNATTENDED_JSON)
    _assert_blocks_identical(steps_a, steps_b)


def test_drift_guard_catches_intentional_drift():
    """Mutate one shared step in-memory and confirm the comparator raises."""
    steps_a = _load_steps_by_name(OPTIMIZE_JSON)
    steps_b = _load_steps_by_name(OPTIMIZE_UNATTENDED_JSON)
    mutated = copy.deepcopy(steps_a)
    mutated["setup"]["mode"] = "drifted-mode-for-test"
    with pytest.raises(AssertionError, match="DRIFT in step 'setup'"):
        _assert_blocks_identical(mutated, steps_b)


def test_experimentation_step_diverges_intentionally():
    """The one step that MUST differ — guard against accidentally syncing it."""
    steps_a = _load_steps_by_name(OPTIMIZE_JSON)
    steps_b = _load_steps_by_name(OPTIMIZE_UNATTENDED_JSON)
    assert "experimentation" in steps_a
    assert "experimentation" in steps_b
    assert _serialize(steps_a["experimentation"]) != _serialize(steps_b["experimentation"]), (
        "experimentation step is identical — the two protocols must diverge here "
        "(batch checkpoint vs autonomous loop)."
    )
