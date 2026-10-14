---
title: Review Loop Convergence
tags: [protocols, agents, testing]
created: 2026-10-13
updated: 2026-10-13
sources: [sub-protocols/code-review.md, code-review.md, protocol_engine.py]
---

# Review Loop Convergence

Why the code-review step is built to end, and the pieces that make it end. Mechanics live in `sub-protocols/code-review.md`; the gate code in `protocol_engine.py` (`_compute_review_tree_hash`, `_func_verify_review_round`, `_func_require_spec_review_passed`).

## The loop it replaces
Three rules once combined into a review that never finished in strict mode (`code_review.enforce_warnings: true`):
- **A wide Warning band.** Warnings were High + Medium, and Medium included "deviation from an established pattern". Almost any remark about a test fit there.
- **Strict mode fixed every warning and re-ran the review**, without asking anyone.
- **Every round started from zero.** Fresh reviewers had no memory of earlier rounds, and a fresh reader always finds a new weakness, often one the previous fix created.

On top of that, findings were applied as verdicts: aggregated "with equal weight", then "fix all identified issues".

## Design (each part closes one cause)
- **Three-level scale** (Critical / Warning / Note), shared by every code-review reviewer and both provider templates. Warning = a defect, or behaviour/criterion no test exercises *at all*. "A test could assert more" is a Note. Notes never block. This is the load-bearing change: without it the other rules cannot break the loop, because strict mode keeps fixing "test improvements" as warnings. See [Review Output Contract](pages/entities/review-output-contract.md).
- **Claim, not verdict.** The orchestrator confirms each finding in the code before acting. Unconfirmed or downgraded findings are recorded with the reason and drive no change.
- **Routing by fix depth.** SIMPLE (one file, no logic change) is fixed in the step; the test covering the fix does not count as a second file, and a changed default, flag, branch or condition is a logic change. COMPLEX (several files or a logic change) goes back to the protocol's code-writing step. AMBIGUOUS goes to the planning step, where the user decides. The deepest depth wins. Targets differ per protocol: `task` → implementation/investigation, `refactoring` → refactoring/planning, optimize → experimentation or a direct question.
- **History in every re-review.** Each reviewer gets the scale and the previous rounds' findings with their dispositions, written into the prompt itself: agy can read only the repository, so a history file in the orchestrator's scratchpad leaves it nothing to review. The job is to check closure and regressions, not to reopen settled ground.
- **Spec audit last.** The spec-compliance audit runs once, on the final tree, after the last clean round. It no longer re-runs after every review fix.
- **Freshness by tree hash.** `save_note` stamps `REVIEW_ROUND:`, `SPEC_REVIEW: PASSED` and `SPEC_DEFERRED: PASSED` notes with a hash of HEAD + tracked diff + untracked files, excluding `team-management/`, `.claude/` and `.agents/`. The gates pass only while the tree matches, so an unchanged tree is never reviewed twice and a changed tree never passes on an old review. A gate rejection with no code change keeps the round valid. The documentation-step gate `require_deferred_criteria_done` uses the same hash for the deferred-criteria re-audit.

## Observed behaviour
The task that introduced this design was reviewed under it. Its warnings per round narrowed: 4 → 6 → 2 → 1 → 0. The round-2 rise came from the red-team second echelon joining.

One edge case kept coming back in new shapes: managed-block marker pairing in the CLAUDE.md line counter. That was the signal to fix the class instead of the instance. The counter dropped its pairing buffer altogether; it excludes only marker lines and `@`-imports inside a block. When the same defect class reappears after a fix, rework the mechanism rather than patch the next shape.

## Gotchas
- The hash anchors on `HEAD`. A repository with no commits cannot pass either gate until it has an initial commit; the gate message names this remedy.
- `wiki/` is part of the hash, so an in-step wiki touch-up during code-review makes the round stale. Do wiki edits in the documentation step.
- A sentinel written by an older version has no `tree_hash` key. It falls back to the timestamp check against backward gotos. A note that has the key with a null value is stale.
- Custom protocol forks keep their copied sub-protocol. Run `/team-management:custom-protocol-update-after-reinstall` to pick up the new order.

## Related
- [Workflow Protocols](pages/protocols/workflow-protocols.md) — the two-stage gate in each protocol.
- [CLAUDE.md Hygiene](pages/topics/claude-md-hygiene.md) — the documentation-step gate introduced alongside this design.
