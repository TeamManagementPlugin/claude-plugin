"""Drift guard for the code-review loop contract.

The loop ends because of four rules working together: re-review rounds carry
the history, findings are confirmed before they drive work, routing follows fix
depth, and only Warnings in the three-level sense block in strict mode. The
spec audit runs once, last, and both review stages are bound to the tree they
saw through the engine-stamped tree hash.
"""

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CONFIGS = REPO / "plugin" / "protocol-configs"
SUB = CONFIGS / "sub-protocols" / "code-review.md"
REVIEW_PROTOCOLS = ["task", "refactoring", "optimize", "optimize-unattended"]


def _text():
    return re.sub(r"\s+", " ", SUB.read_text(encoding="utf-8"))


def _code_review_step(protocol):
    data = json.loads((CONFIGS / f"{protocol}.json").read_text(encoding="utf-8"))
    return next(step for step in data["steps"] if step["name"] == "code-review")


# --- SC-4: re-review rounds carry the history ------------------------------

def test_re_review_prompts_carry_scale_and_previous_rounds():
    text = _text()
    assert "Re-review rounds carry the history." in text
    assert "## Previous rounds" in text
    assert "three-level severity scale" in text
    assert "does not reopen settled ground" in text


def test_history_travels_inside_the_prompt():
    """agy can read only the repository: a history file in the orchestrator's
    scratchpad left it with nothing to review in rounds 2 and 3 of a live run."""
    text = _text()
    assert "never as a path to a file outside the repository" in text
    assert "agy can read only the repository" in text
    assert "re-dispatch agy with that content inline" in text


def test_every_round_still_dispatches_every_reviewer():
    assert "Every re-review round repeats this parallel dispatch" in _text()


# --- SC-5: claim, not verdict; routing by depth ----------------------------

def test_findings_are_claims_confirmed_by_the_orchestrator():
    text = _text()
    assert "A reader's finding is a claim, not a verdict." in text
    assert "Confirm it in the code yourself." in text
    assert "is recorded as such with the reason" in text
    assert "Fix all identified issues" not in text


def test_routing_by_fix_depth():
    text = _text()
    assert "**SIMPLE**" in text and "**COMPLEX**" in text and "**AMBIGUOUS**" in text
    assert "the deepest wins" in text


def test_simple_excludes_behaviour_changes():
    """A changed default fixed with its test is two files and new behaviour; it
    was routed as SIMPLE in a live run."""
    text = _text()
    assert "The test that covers a fix is not a second file." in text
    assert "a different default" in text
    assert "COMPLEX even when it is one line" in text


# Every goto target the routing table names must exist in that protocol,
# or a COMPLEX/AMBIGUOUS route fails in goto_step.
ROUTING_TARGETS = {
    "task": ["implementation", "investigation"],
    "refactoring": ["refactoring", "planning"],
    "optimize": ["experimentation"],
    "optimize-unattended": ["experimentation"],
}


@pytest.mark.parametrize("protocol", REVIEW_PROTOCOLS)
def test_routing_targets_exist_in_each_protocol(protocol):
    raw = SUB.read_text(encoding="utf-8")
    data = json.loads((CONFIGS / f"{protocol}.json").read_text(encoding="utf-8"))
    steps = {step["name"] for step in data["steps"]}
    label = "`optimize` / `optimize-unattended`" if protocol.startswith("optimize") else f"`{protocol}`"
    row = next(line for line in raw.splitlines() if line.strip().startswith(f"| {label} |"))
    # Outside the table no step name may be hard-coded: three of the four
    # consumers lack task's implementation/investigation steps.
    outside = raw.replace(row, "")
    assert 'step_name="implementation"' not in outside
    assert 'step_name="investigation"' not in outside
    assert "through investigation" not in outside
    for target in ROUTING_TARGETS[protocol]:
        assert f"`{target}`" in row, f"{protocol}: routing row lost `{target}`"
        assert target in steps, f"{protocol}: routing target `{target}` is not a step"


def test_each_round_is_recorded_with_review_round_note():
    assert 'note="REVIEW_ROUND: ' in _text()


# --- SC-6: strict mode blocks on Warnings only -----------------------------

def test_strict_mode_blocks_only_on_warnings():
    text = _text()
    assert "Fix ALL warnings automatically" not in text
    assert "Notes never block and never trigger a re-review" in text


@pytest.mark.parametrize("protocol", REVIEW_PROTOCOLS)
def test_end_text_has_no_fix_everything_loop(protocol):
    end = _code_review_step(protocol)["end"]
    assert "until clean" not in end
    assert "notes never block" in end.lower()
    assert "strict (enforce_warnings=true) → zero confirmed warnings" in end


# --- SC-7: code review first, spec audit last ------------------------------

def test_spec_audit_section_follows_code_review_rounds():
    raw = SUB.read_text(encoding="utf-8")
    review = raw.index("## 2. Run Code Review Agent")
    confirm = raw.index("## 3. Confirm and Route Findings")
    spec = raw.index("## 6. Spec Compliance Audit (LAST")
    assert review < confirm < spec


def test_manual_re_save_rule_replaced_by_structural_freshness():
    text = _text()
    assert "re-save the sentinel" not in text
    assert "Freshness is structural." in text


@pytest.mark.parametrize("protocol", REVIEW_PROTOCOLS)
def test_spec_gate_is_post_only_and_after_review_round(protocol):
    step = _code_review_step(protocol)
    assert "require_spec_review_passed" not in step["pre_funcs"]
    post = step["post_funcs"]
    assert post[0] == "verify_review_round"
    assert post[1] == "require_spec_review_passed"
    assert step["post_funcs_stop_on_failure"] is True


# --- SC-6: the MCP enforcement guidance agrees with §5 ---------------------

def test_strict_guidance_offers_no_proceed_anyway_and_spares_notes():
    import sys
    sys.path.insert(0, str(REPO / "plugin" / "hooks"))
    from protocol_utils import get_review_completion_language

    strict = get_review_completion_language(True)
    text = " ".join(strict.values()).lower()
    assert "proceed anyway" not in text
    assert "confirmed warning" in text
    assert "notes never block" in text

    relaxed = " ".join(get_review_completion_language(False).values()).lower()
    assert "notes never block" in relaxed
