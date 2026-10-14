"""Drift guard for the three-level review severity scale.

Every reviewer that feeds the code-review step rates findings on one scale:
Critical / Warning / Note. Warning means a defect (or behaviour no test
exercises at all); everything softer is a Note and never blocks. A fourth
level, or a wider Warning band, re-opens the review loop this scale exists
to close.
"""

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
AGENTS = REPO / "plugin" / "agents"
PROVIDERS = REPO / "plugin" / "protocol-configs" / "providers"
SUB_PROTOCOL = REPO / "plugin" / "protocol-configs" / "sub-protocols" / "code-review.md"

REVIEW_AGENTS = ["code-review.md", "testing-specialist.md", "red-team.md", "code-cleanliness.md"]
PROVIDER_TEMPLATES = ["codex-code-review.md", "agy-code-review.md"]
JSONL_CARRIERS = [AGENTS / "code-review.md", AGENTS / "testing-specialist.md", AGENTS / "red-team.md"]

ALL_CARRIERS = [AGENTS / n for n in REVIEW_AGENTS] + [PROVIDERS / n for n in PROVIDER_TEMPLATES]

# Phrases that define each level. Matched case-insensitively against the
# carrier text with whitespace collapsed.
CRITICAL_MARKERS = ["security fault", "data loss", "crash", "broken contract", "unmet success criterion"]
WARNING_MARKERS = ["a defect", "no test exercises at all"]
NOTE_MARKERS = [
    "a test that could assert more",
    "a mutation a test would let through",
    "naming",
    "a finding in code the change did not touch",
    "notes never block",
]

# Retired labels: the 4-level scale's severity names and its wide Warning band.
RETIRED = [
    re.compile(r"🟠\s*\**High"),
    re.compile(r"🟡\s*\**Medium"),
    re.compile(r"🟢\s*\**Low"),
    re.compile(r"\*\*Severity\*\*:\s*\[?(?:High|Medium|Low)\b"),
    re.compile(r"High\s*\+\s*Medium"),
    re.compile(r"\[(?:HIGH|MEDIUM|LOW)\]"),
    re.compile(r"\[P[123]\]"),
]


def _flat(path: Path) -> str:
    return re.sub(r"\s+", " ", path.read_text(encoding="utf-8")).lower()


@pytest.mark.parametrize("path", ALL_CARRIERS, ids=lambda p: p.name)
def test_carrier_defines_three_levels(path):
    text = _flat(path)
    for marker in CRITICAL_MARKERS + WARNING_MARKERS + NOTE_MARKERS:
        assert marker in text, f"{path.name}: scale definition missing '{marker}'"


@pytest.mark.parametrize("path", ALL_CARRIERS + [SUB_PROTOCOL], ids=lambda p: p.name)
def test_retired_labels_are_gone(path):
    raw = path.read_text(encoding="utf-8")
    for pattern in RETIRED:
        hit = pattern.search(raw)
        assert hit is None, f"{path.name}: retired severity label {hit.group(0)!r}"


def _jsonl_blocks(raw: str):
    return re.findall(r"```jsonl\n(.*?)```", raw, flags=re.DOTALL)


@pytest.mark.parametrize("path", JSONL_CARRIERS, ids=lambda p: p.name)
def test_jsonl_examples_use_three_severities(path):
    blocks = _jsonl_blocks(path.read_text(encoding="utf-8"))
    assert blocks, f"{path.name}: no ```jsonl example block"
    seen = 0
    for block in blocks:
        for line in block.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            record = json.loads(line)
            assert record["severity"] in {"critical", "warning", "note"}, (
                f"{path.name}: JSONL severity {record['severity']!r}"
            )
            seen += 1
    assert seen, f"{path.name}: no JSONL finding lines parsed"


def test_sub_protocol_schema_names_three_severities():
    raw = SUB_PROTOCOL.read_text(encoding="utf-8")
    assert "`critical` \\| `warning` \\| `note`" in raw
    assert "`high`" not in raw and "`medium`" not in raw


def test_cleanliness_no_longer_claims_shared_scale_with_analysts():
    text = _flat(AGENTS / "code-cleanliness.md")
    assert "risk-security-analyst" not in text
