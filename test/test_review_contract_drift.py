#!/usr/bin/env python3
"""Drift-guard: the review-agent output contract stays present in the carrier prompts.

h-gstack-review-contract introduced a unified output contract for the review
agents — confidence calibration, the pre-emit quote-gate, AUTO-FIX/ASK
classification, FP suppressions, and the machine-readable JSONL findings
schema (fingerprint + reserved verdict). The contract is prompt-text only, so
nothing structural fails if a future edit silently drops a section — these
content-level assertions are the regression net (same pattern as
test_code_review_heading_gates.py for the gate-parsed headings).

Run with: python3 -m pytest test/test_review_contract_drift.py -v
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AGENT = REPO / "plugin" / "agents" / "code-review.md"
SUBPROTO = REPO / "plugin" / "protocol-configs" / "sub-protocols" / "code-review.md"
CRITIC = REPO / "plugin" / "agents" / "critic.md"
RISK = REPO / "plugin" / "agents" / "risk-security-analyst.md"
TESTING = REPO / "plugin" / "agents" / "testing-specialist.md"
REDTEAM = REPO / "plugin" / "agents" / "red-team.md"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# --- code-review agent: contract sections [SC-1..SC-3] ---

def test_agent_contract_sections_present():
    text = _read(AGENT)
    for heading in (
        "### Confidence Calibration (1–10)",
        "### Quote-Gate (Pre-Emit)",
        "### AUTO-FIX vs ASK",
        "### DO NOT Flag (FP Suppressions)",
        "## 📎 Appendix — Low Confidence (",
        "## Machine-Readable Findings",
    ):
        assert heading in text, f"code-review.md lost contract section {heading!r}"


def test_agent_finding_line_format():
    # [SEV] (confidence: N/10) file:line — description
    assert re.search(r"\[(?:CRITICAL|WARNING|NOTE)\] \(confidence: \d+/10\)", _read(AGENT)), (
        "code-review.md lost the per-finding confidence line format example"
    )


def test_agent_no_quote_confidence_cap():
    text = _read(AGENT)
    assert "CAPPED at confidence 5" in text, "quote-gate confidence cap missing"
    assert "never 6+" in text, "no-quote-never-6+ rule missing"


def test_agent_critical_exception_keeps_gate_visibility():
    # codex P1: a low-confidence Critical must stay in the gate-parsed
    # '## 🔴 Critical Issues (N)' section — the completion gate reads only that
    # heading, so an appendix-buried Critical would let completion pass.
    text = _read(AGENT)
    assert "Critical exception" in text, "Critical placement exception missing"
    assert "NEVER moved to the appendix" in text, "Critical never-appendix rule missing"


def test_agent_displayed_examples_carry_quote_fences():
    # Structural count: only '**Quote**:' immediately followed by a fence line
    # (the example fences) — prose mentions of the field name cannot dilute it.
    assert _read(AGENT).count("**Quote**:\n```") >= 3, (
        "output-format examples must model the quote-gate (3 fenced **Quote**: examples)"
    )


def test_secrets_redaction_rule_present_in_both_carriers():
    # codex round-2 P1: a credential-exposure finding must not reproduce the
    # secret verbatim — the review reaches the work log and MR/PR comments.
    assert "[REDACTED]" in _read(AGENT), "code-review.md lost the secrets-redaction quote rule"
    assert "[REDACTED]" in _read(SUBPROTO), "sub-protocol lost the secrets-stay-redacted rule"


def test_column0_hash_rule_present_in_both_carriers():
    # Quote fences containing a column-0 '#' line (shebang, #include, comment)
    # truncate MR/PR extraction — both carriers must state the rule.
    assert "column 0" in _read(AGENT), "code-review.md lost the column-0 # quote rule"
    assert "column 0" in _read(SUBPROTO), "sub-protocol lost the column-0 # quote rule"


def test_agent_no_findings_sentinel():
    assert "NO FINDINGS" in _read(AGENT)


# --- sub-protocol: JSONL contract [SC-4] ---

def test_subproto_jsonl_section_present():
    assert "## 4a. Machine-Readable Findings Contract (JSONL)" in _read(SUBPROTO)


def test_subproto_schema_fields_present():
    text = _read(SUBPROTO)
    for field in ("`severity`", "`confidence`", "`path`", "`line`", "`category`",
                  "`summary`", "`fix`", "`fingerprint`", "`verdict`", "`test_stub`"):
        assert field in text, f"sub-protocol JSONL schema lost field {field}"
    assert '"{path}:{line}:{category}"' in text, "fingerprint recipe missing"
    assert "NO FINDINGS" in text


def test_subproto_jsonl_is_internal_and_verdict_reserved():
    text = _read(SUBPROTO)
    assert "NEVER appended" in text, "JSONL-is-internal rule missing"
    assert "always `null` in v1" in text, "reserved-verdict rule missing"


# --- risk-security-analyst: status labels [SC-5] ---

def test_risk_status_labels():
    text = _read(RISK)
    assert "**Status**: VERIFIED / UNVERIFIED / TENTATIVE" in text
    for label in ("**VERIFIED**", "**UNVERIFIED**", "**TENTATIVE**"):
        assert label in text, f"risk-security-analyst.md lost status definition {label}"


# --- critic: fresh context + canonical verdict [SC-6] ---

def test_critic_fresh_context_declaration():
    assert "Fresh context, no prior review bias." in _read(CRITIC)


def test_critic_canonical_verdict_line():
    assert "Recommendation: <action> because <one-line reason naming a specific finding>" in _read(CRITIC)


# --- new chain agents carry the full contract [m-gstack-agent-roster] ---

def test_new_agents_carry_contract_sections():
    for path in (TESTING, REDTEAM):
        text = _read(path)
        for marker in (
            "### Confidence Calibration (1–10)",
            "### Quote-Gate (Pre-Emit)",
            "### AUTO-FIX vs ASK",
            "## Machine-Readable Findings",
            "NO FINDINGS",
            "[REDACTED]",
            "column 0",
            "NEVER moved to the appendix",
        ):
            assert marker in text, f"{path.name} lost contract marker {marker!r}"


def test_new_agents_finding_line_format():
    for path in (TESTING, REDTEAM):
        assert re.search(r"\[(?:CRITICAL|WARNING|NOTE)\] \(confidence: \d+/10\)", _read(path)), (
            f"{path.name} lost the per-finding confidence line format"
        )


def test_new_agents_examples_carry_quote_fences():
    # Structural count, same rule as the code-review agent's guard.
    for path in (TESTING, REDTEAM):
        assert _read(path).count("**Quote**:\n```") >= 2, (
            f"{path.name} output-format examples must model the quote-gate"
        )


def test_testing_specialist_focus_areas():
    text = _read(TESTING).lower()
    for marker in ("negative-path", "isolation", "flaky", "changed behaviour with no test"):
        assert marker in text, f"testing-specialist.md lost focus area {marker!r}"


def test_red_team_second_echelon_mechanics():
    text = _read(REDTEAM)
    assert "SECOND ECHELON" in text, "red-team.md lost the second-echelon framing"
    assert "DO NOT duplicate round 1" in text, "red-team.md lost the no-duplicate rule"
    assert "merged first-round findings" in text.lower() or "merged round-1" in text.lower(), (
        "red-team.md lost the merged-findings input contract"
    )
