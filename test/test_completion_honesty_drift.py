#!/usr/bin/env python3
"""Drift-guard: the completion-honesty vocabulary stays present in the carrier prompts.

m-gstack-completion-honesty introduced per-item Verification Modes, five
statuses, the honesty rule, Investigation Depth, the SPEC_UNVERIFIABLE carriage
note, per-item UNVERIFIABLE confirmation, and the fail-open prohibition. The
contract is prompt-text only, so nothing structural fails if a future edit
silently drops a section — these content-level assertions are the regression
net (same pattern as test_review_contract_drift.py).

Run with: python3 -m pytest test/test_completion_honesty_drift.py -v
"""

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REVIEWER = REPO / "plugin" / "agents" / "spec-compliance-reviewer.md"
CODE_REVIEW = REPO / "plugin" / "protocol-configs" / "sub-protocols" / "code-review.md"
COMPLETION = REPO / "plugin" / "protocol-configs" / "sub-protocols" / "task-completion.md"

VERIFICATION_MODES = ("DIFF-VERIFIABLE", "CROSS-REPO", "EXTERNAL-STATE", "CONTENT-SHAPE")
STATUSES = ("DONE", "PARTIAL", "NOT DONE", "CHANGED", "UNVERIFIABLE", "DEFERRED")
INVESTIGATION_CAUSES = (
    "scope cut",
    "context exhaustion",
    "misunderstood requirement",
    "blocked by dependency",
    "genuinely forgotten",
)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# --- spec-compliance-reviewer.md: status vocabulary [SC-1..SC-3] ---

def test_reviewer_all_verification_modes_present():
    text = _read(REVIEWER)
    for mode in VERIFICATION_MODES:
        assert mode in text, f"spec-compliance-reviewer.md lost Verification Mode {mode!r}"


def test_reviewer_all_statuses_present():
    text = _read(REVIEWER)
    for status in STATUSES:
        assert f"`{status}`" in text, f"spec-compliance-reviewer.md lost status token `{status}`"


def test_reviewer_honesty_rule():
    text = _read(REVIEWER)
    assert "Code that handles a deliverable is not the deliverable" in text, (
        "honesty rule phrase missing"
    )
    assert "When in doubt between DONE and UNVERIFIABLE, choose UNVERIFIABLE" in text, (
        "doubt→UNVERIFIABLE tie-break missing"
    )


def test_reviewer_content_shape_rule():
    text = _read(REVIEWER)
    assert "shape-phrased" in text, "CONTENT-SHAPE shape-phrased rule missing"
    # codex round-3 P2: the UNVERIFIABLE definition must cover the CONTENT-SHAPE
    # case (artifact IS in the diff; the semantic judgement is what the diff
    # cannot prove) — not only evidence-outside-the-repo modes.
    assert "cannot fully establish the criterion" in text, (
        "UNVERIFIABLE definition regressed to evidence-outside-the-diff only — "
        "it must also cover semantic CONTENT-SHAPE criteria"
    )


def test_reviewer_investigation_depth_closed_list():
    text = _read(REVIEWER)
    for cause in INVESTIGATION_CAUSES:
        assert cause in text, f"Investigation Depth cause {cause!r} missing"
    assert "IMPACT: HIGH|MEDIUM|LOW" in text, "IMPACT rating scale missing"


def test_reviewer_strict_verdict_derivation():
    text = _read(REVIEWER)
    assert "Any `PARTIAL` / `NOT DONE` / `CHANGED` → FAIL" in text, (
        "strict verdict-derivation rule (D2) missing"
    )
    assert "every criterion is `DONE`, `UNVERIFIABLE` or `DEFERRED`" in text, (
        "PASS condition (all DONE, UNVERIFIABLE or DEFERRED) missing"
    )


def test_reviewer_unverifiable_items_section():
    assert "## Unverifiable Items" in _read(REVIEWER), (
        "## Unverifiable Items output section missing from reviewer template"
    )


def test_reviewer_changed_disclosure_format():
    text = _read(REVIEWER)
    assert "spec says <X>, diff does <Y>" in text, "CHANGED disclosure example missing"


def test_reviewer_mode_dodging_antipattern():
    text = _read(REVIEWER)
    assert "Optimistic mode classification" in text, (
        "mode-dodging anti-pattern missing"
    )
    # codex round-5 P1: classification keys on the deliverable, in BOTH
    # directions — external state change stays EXTERNAL-STATE, but in-repo
    # integration code stays DIFF-VERIFIABLE (never laundered into
    # UNVERIFIABLE by pointing at the system it talks to).
    assert "keys on what the criterion DELIVERS" in text, (
        "deliverable-based mode classification rule missing"
    )
    assert "launder" in text, (
        "inverse over-classification (code → UNVERIFIABLE laundering) guard missing"
    )


# --- code-review.md: status handling + carriage + audit failure [SC-1, SC-5] ---

def test_subproto_status_handling_table():
    text = _read(CODE_REVIEW)
    for status in STATUSES:
        assert f"`{status}`" in text, f"code-review.md §1 handling table lost `{status}`"


def test_subproto_carriage_note_format():
    text = _read(CODE_REVIEW)
    assert 'SPEC_UNVERIFIABLE: SC-x, SC-y' in text, (
        "canonical SPEC_UNVERIFIABLE carriage note example missing"
    )
    assert "SPEC_UNVERIFIABLE: none" in text, (
        "explicit-none carriage rule missing (stale-note supersession)"
    )
    assert "on EVERY PASS" in text or "Save it on EVERY PASS" in text, (
        "save-on-every-PASS rule missing"
    )


def test_subproto_audit_failure_rule():
    text = _read(CODE_REVIEW)
    assert "NEITHER PASS NOR FAIL" in text, "audit-failure classification missing"
    assert 'recording `SPEC_REVIEW: PASSED` without a real PASS verdict is prohibited' in text, (
        "sentinel-without-verdict prohibition missing"
    )
    # codex round-2 P2: a proceed-without-audit branch is structurally impossible
    # (require_spec_review_passed blocks advance without the sentinel) — the
    # paragraph must name protocol_abort as the real alternative, not a dead end.
    assert "NO proceed-without-audit path" in text, (
        "no-bypass-by-design statement missing from the audit-failure rule"
    )
    assert "protocol_abort" in text, (
        "the real alternative (protocol_abort) missing from the audit-failure rule"
    )


def test_subproto_sentinel_untouched():
    # SC-6: the exact sentinel string must remain present (engine parses it verbatim).
    assert 'SPEC_REVIEW: PASSED' in _read(CODE_REVIEW), "SPEC_REVIEW: PASSED sentinel lost"


# --- task-completion.md: per-item confirms + fail-open [SC-4, SC-5] ---

def test_completion_per_item_confirmation_section():
    text = _read(COMPLETION)
    assert "## 1a. Per-Item Confirmation of UNVERIFIABLE Items" in text, (
        "Section 1a missing"
    )
    assert "BEFORE any generic confirmation" in text, (
        "per-item-before-generic placement rule missing"
    )


def test_completion_blanket_confirm_prohibited():
    text = _read(COMPLETION)
    assert "Blanket confirmation is prohibited" in text, "blanket-confirm prohibition missing"


def test_completion_multiselect_batching():
    text = _read(COMPLETION)
    assert "More than 3 items" in text, "batching threshold (>3) missing"
    assert "multiSelect: true" in text, "multiSelect batching rule missing"
    assert "at most 4 options" in text, "widget option-limit grouping rule missing"


def test_completion_fail_open_prohibition():
    text = _read(COMPLETION)
    assert "## 1b. Fail-Open Prohibition" in text, "fail-open prohibition section missing"
    assert '"Stop and fix"' in text, "stop-and-fix recommended default missing"
    assert "Absence of a verdict is not a verdict" in text, (
        "could-not-run vs failed distinction missing"
    )
    # Round-2 W1: 1b's trigger must include 1a's routing explicitly, or its own
    # AND-condition lets a reader conclude the question is optional in the most
    # likely failure mode (carriage note forgotten, other evidence present).
    assert "Section 1a routed here" in text, (
        "1a-routing trigger missing from Section 1b — the two sections must compose"
    )


def test_completion_carriage_staleness_check():
    # codex round-4 P1: a session dying between the sentinel save and the
    # carriage save must not let an older round's note masquerade as current —
    # a carriage note older than the latest sentinel is treated as absent.
    text = _read(COMPLETION)
    assert "Staleness check first" in text, (
        "carriage-note staleness check missing from Section 1a step 1"
    )
    assert "OLDER than the latest sentinel" in text, (
        "stale-note (older than latest sentinel) rule missing"
    )
    # Round-5 refinement (Claude Low note): a stale note PROVES the interruption,
    # so the superseded round's carry-list must not re-enter via the fallback —
    # stale goes straight to 1b, the carry-list fallback is reserved for the
    # genuinely-absent-note case.
    assert "Section 1b applies directly" in text, (
        "stale-note direct-to-1b routing missing (stale carry-list must not be served)"
    )


def test_completion_id_set_loads_carry_list():
    # codex round-5 P2: for a fresh SC-ID note the step must direct the reader
    # to the work-log carry-list — the note has only IDs, but step 2's questions
    # must name each item's Verification Mode, reason, and what to check.
    text = _read(COMPLETION)
    assert "recorded at PASS time for every non-empty set" in text, (
        "carry-list loading instruction for fresh SC-ID notes missing"
    )
    assert "A fresh ID note WITHOUT a matching carry-list" in text, (
        "fresh-ID-note-without-carry-list inconsistency branch missing"
    )


def test_subproto_carry_list_carries_mode():
    # codex round-4 P2: after compaction the per-item questions must name each
    # item's Verification Mode — the work-log carry-list is the mode source.
    text = _read(CODE_REVIEW)
    assert "its Verification Mode + why it is unverifiable" in text, (
        "carry-list line format lost the Verification Mode field"
    )


def test_completion_unverifiable_note_source():
    text = _read(COMPLETION)
    assert "SPEC_UNVERIFIABLE:" in text, (
        "task-completion.md lost the SPEC_UNVERIFIABLE note as the set's source of truth"
    )
    # codex round-3 P1 (partial adopt): a post-compaction orchestrator must know
    # the per-SC status list is derivable from the notes — strict verdict means
    # everything outside the carriage set is DONE by construction.
    assert "reconstructible from the notes alone" in text, (
        "post-compaction status-reconstruction rule missing from Section 1"
    )


# --- codex plan-review adoptions: batching ceiling, legacy carriage, ordering, scope ---

def test_completion_over_16_items_fallback():
    # codex: a single AskUserQuestion call caps at 4 questions x 4 options = 16 items;
    # 17+ needs a defined fallback, not an unrepresentable dead end.
    text = _read(COMPLETION)
    assert "More than 16 items" in text, "over-16-items fallback missing"
    assert "successive multiSelect calls" in text, "successive-calls fallback rule missing"


def test_completion_section_ordering():
    # Per-item confirmation (1a) and fail-open (1b) must precede user testing (2)
    # and generic confirmation (3) — ordering is the whole point of Section 1a.
    text = _read(COMPLETION)
    idx_1a = text.find("## 1a. Per-Item Confirmation of UNVERIFIABLE Items")
    idx_1b = text.find("## 1b. Fail-Open Prohibition")
    idx_2 = text.find("## 2. User Testing Phase")
    idx_3 = text.find("## 3. User Confirmation")
    assert -1 not in (idx_1a, idx_1b, idx_2, idx_3), (
        "task-completion.md lost one of sections 1a / 1b / 2 / 3"
    )
    assert idx_1a < idx_1b < idx_2 < idx_3, (
        "task-completion.md section order broken: 1a must precede 1b, 2, and 3"
    )


def test_completion_scope_covers_all_four_consumers():
    # Round-1 reviewers (Claude Critical + codex P1) both caught the original
    # scope guard falsely excluding refactoring — refactoring.json DOES run the
    # spec-compliance stage (require_spec_review_passed in pre+post funcs).
    # The scope must be capability-phrased and name all four shipped consumers.
    text = _read(COMPLETION)
    assert "`task`, `refactoring`, `optimize`, `optimize-unattended`" in text, (
        "Section 1a scope must name all four shipped consumers of this sub-protocol"
    )
    assert "produces no UNVERIFIABLE classifications" in text, (
        "custom-fork (no spec stage) skip rule missing from Section 1a"
    )


def test_completion_fail_closed_on_missing_carriage():
    # codex P1-2: a forgotten carriage note is indistinguishable from a
    # pre-feature run — the chain must fail toward the 1b question, never
    # silently absorb the absence as an empty set.
    text = _read(COMPLETION)
    assert "do NOT assume an empty set" in text, (
        "fail-closed rule for missing carriage note/carry-list is gone"
    )
    assert "audit-data inconsistency" in text, (
        "legacy-note-without-carry-list inconsistency branch missing"
    )


def test_subproto_legacy_carriage():
    text = _read(CODE_REVIEW)
    assert "SPEC_UNVERIFIABLE: legacy — see work log" in text, (
        "legacy (no SC IDs) carriage note format missing"
    )
    assert "append the carry-list to the task work log" in text, (
        "work-log carry-list rule missing"
    )
    assert "only when the set is non-empty" in text, (
        "legacy variant must be conditioned on a non-empty set (W1: an empty-set "
        "legacy task saves 'none', never a pointer to a carry-list that was not written)"
    )


def test_subproto_carriage_note_after_sentinel():
    # The carriage note step must come after the sentinel step — the sentinel is
    # the gate, the carriage note is its companion, never a substitute.
    text = _read(CODE_REVIEW)
    idx_sentinel = text.find('note="SPEC_REVIEW: PASSED"')
    idx_carriage = text.find('note="SPEC_UNVERIFIABLE: SC-x, SC-y"')
    assert -1 not in (idx_sentinel, idx_carriage), (
        "code-review.md lost the sentinel or carriage protocol_save_note call literal"
    )
    assert idx_sentinel < idx_carriage, (
        "carriage-note step must follow the sentinel step in code-review.md §1"
    )


# --- DEFERRED: documentation criteria owned by a later step ---
# The task protocol writes documentation after code-review, so an audit at the
# end of code-review cannot see a documentation criterion done. A live run
# recorded PASS anyway on a promise; DEFERRED makes the deferral explicit and
# the documentation step proves it (test/test_spec_deferred_gate.py).

def test_reviewer_deferred_eligibility():
    text = _read(REVIEWER)
    assert "names a later protocol step that owns documentation" in text, (
        "DEFERRED must require a later step named by the orchestrator"
    )
    assert "never code or tests" in text, "DEFERRED must exclude code and tests"
    assert "names no later step" in text, (
        "no later step named → the item stays NOT DONE"
    )
    # codex plan review: a criterion with a missing code/test part cannot hide
    # behind its documentation part.
    assert "every part still missing is a documentation deliverable" in text, (
        "mixed code-plus-docs criterion rule missing"
    )


def test_reviewer_deferred_items_section():
    assert "## Deferred Items" in _read(REVIEWER), (
        "## Deferred Items output section missing from reviewer template"
    )


def test_reviewer_deferred_check_mode():
    text = _read(REVIEWER)
    assert "Deferred-check mode" in text, "deferred-check mode missing"
    assert "audit only those" in text, "deferred-check mode must audit only the listed IDs"


def test_subproto_names_the_later_step():
    text = _read(CODE_REVIEW)
    assert "Later documentation step" in text, (
        "code-review.md §6 must tell the orchestrator to name the later step"
    )
    assert "`task`: `documentation`" in text, "task's later step not named"
    assert "nothing may be deferred" in text, (
        "refactoring/optimize must defer nothing"
    )


def test_subproto_deferred_carriage_note():
    text = _read(CODE_REVIEW)
    assert "SPEC_DEFERRED: SC-x, SC-y" in text, "SPEC_DEFERRED carriage example missing"
    assert "SPEC_DEFERRED: none" in text, "explicit-none SPEC_DEFERRED rule missing"


def test_completion_counts_confirmed_deferred_items_done():
    text = _read(COMPLETION)
    assert "SPEC_DEFERRED: PASSED" in text, (
        "task-completion.md must treat confirmed deferred items as DONE"
    )


def test_subproto_later_step_keyed_on_the_gate():
    """A fork reconciled file by file can keep a documentation step without the
    gate; the later step is the one that runs it, not whatever `task` names."""
    text = _read(CODE_REVIEW)
    assert "In a custom protocol check the step's `post_funcs`, not the protocol name" in text


def test_reviewer_frontmatter_allows_the_deferred_check():
    head = _read(REVIEWER).split("---")[1]
    assert "documentation step's deferred check" in head, (
        "the frontmatter still limits the agent to code-review while the "
        "documentation step dispatches it"
    )


def test_no_page_describes_five_statuses():
    stale = [str(p.relative_to(REPO)) for p in (REPO / "wiki" / "pages").rglob("*.md")
             if "five statuses" in p.read_text(encoding="utf-8")]
    assert not stale, f"pages still describe five statuses: {stale}"


def test_completion_requires_a_fresh_deferred_confirmation():
    """A deferred item is DONE only when a SPEC_DEFERRED: PASSED note answers a
    carriage saved for the final audit; otherwise it is unconfirmed."""
    text = _read(COMPLETION)
    assert "newer than a `SPEC_DEFERRED:` list that is itself at-or-after the latest `SPEC_REVIEW: PASSED`" in text
    assert "an unconfirmed deferred item goes to Section 1b" in text


def test_reviewer_defers_only_documentation():
    """Only real documentation may skip the review rounds. In a plugin an agent,
    command, sub-protocol or prompt template is a `.md` file but it is
    behaviour, so deferring "any .md" would write it after every review round.
    A README.rst is out too: documentation mode cannot edit it."""
    text = _read(REVIEWER)
    assert ("a `CLAUDE.md` file in any directory, `README*.md`, `CHANGELOG.md`, "
            "or anything under `wiki/` or `docs/`") in text
    assert "agent, command, skill, sub-protocol or prompt template" in text
    assert "`README.rst`" in text
    assert "any `.md` file" not in text


def test_subproto_requires_the_deferred_carriage():
    text = _read(CODE_REVIEW)
    assert "`require_spec_review_passed` blocks advance until it exists" in text
