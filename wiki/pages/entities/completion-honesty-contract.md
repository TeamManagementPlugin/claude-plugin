---
title: Completion Honesty Contract
tags: [agents, protocols, testing, config]
created: 2026-08-06
updated: 2026-10-13
sources: [plugin/agents/spec-compliance-reviewer.md, plugin/protocol-configs/sub-protocols/code-review.md, plugin/protocol-configs/sub-protocols/task-completion.md]
---

# Completion Honesty Contract

Prompt-level contract (brainstorm block B) that makes completion claims honest and
granular across three carriers: the spec-compliance audit's per-item vocabulary
(`spec-compliance-reviewer.md`), the carriage of unverifiable items out of the
code-review step (`sub-protocols/code-review.md` §6, the spec audit that runs last), and per-item user
confirmation + fail-open prohibition at completion (`sub-protocols/task-completion.md`
§1a/§1b). Sibling of the [Review Output Contract](pages/entities/review-output-contract.md);
drift-guarded by `test_completion_honesty_drift.py` (27 content tests, house
pattern of `test_review_contract_drift.py`). Everything here is prompt text —
engine-side enforcement (note pairing, status parsing) is deferred to Phase 2 by
design.

## Verification Modes and Statuses (the audit vocabulary)

Every Success-Criteria item is classified on two axes, mode BEFORE evidence:

- **Modes** — `DIFF-VERIFIABLE` (default; deliverable fully checkable from the
  repo diff), `CROSS-REPO`, `EXTERNAL-STATE` (deployed env, tracker state,
  executed side effect), `CONTENT-SHAPE` (only shape is checkable, not semantic
  quality). Classification keys on what the criterion **delivers**, not what it
  names: an external state change stays `EXTERNAL-STATE` even when the diff has
  code producing it; in-repo integration code stays `DIFF-VERIFIABLE` — never
  laundered into UNVERIFIABLE by pointing at the system it talks to.
- **Statuses** — `DONE` / `PARTIAL` / `NOT DONE` (supersedes the old
  `NOT COVERED`) / `CHANGED` (requires "spec says X, diff does Y" disclosure) /
  `UNVERIFIABLE` (the diff's evidence cannot fully establish the criterion —
  incl. the semantic half of CONTENT-SHAPE) / `DEFERRED` (a documentation
  deliverable — `CLAUDE.md`, `README*.md`, `CHANGELOG.md`, `wiki/`, `docs/` —
  owned by a step the orchestrator names; an agent, command or sub-protocol
  `.md` is behaviour and is never deferred, so it cannot skip the review rounds; only
  `task` has one, `documentation`, so refactoring and optimize defer nothing. A
  criterion still missing a code or test part is never DEFERRED).
- **Honesty rule** — "code that handles a deliverable is not the deliverable";
  doubt between DONE and UNVERIFIABLE resolves to UNVERIFIABLE.
- **Investigation Depth** — every PARTIAL / NOT DONE carries a cause from a
  closed 5-item list (scope cut / context exhaustion / misunderstood requirement /
  blocked by dependency / genuinely forgotten) + `IMPACT: HIGH|MEDIUM|LOW`.
- **Strict verdict (user decision D2)** — PASS ⇔ every item DONE, UNVERIFIABLE or DEFERRED
  and no scope creep; any PARTIAL / NOT DONE / CHANGED → FAIL. CHANGED is
  reconciled either by aligning code to spec or by a user-approved SC amendment +
  re-review. UNVERIFIABLE never blocks PASS — it is *carried* instead. The
  verdict stays binary, so the `SPEC_REVIEW: PASSED` sentinel and its engine gate
  are untouched.

## The Carriage Chain (review → completion)

**Why DEFERRED exists.** The audit runs at the end of code-review, while `task`
writes documentation in the next step. A documentation criterion was therefore
always `NOT DONE` at audit time, and no review fix could change that; in a live
run the orchestrator recorded PASS anyway on a promise to check later. DEFERRED
makes the deferral explicit, and the later step has to prove it: on every PASS
the orchestrator also saves `SPEC_DEFERRED: SC-x, SC-y` or `SPEC_DEFERRED: none`.
The documentation step re-audits the listed IDs in the reviewer's deferred-check
mode and saves `SPEC_DEFERRED: PASSED`, which `save_note` stamps with the tree
hash. `require_spec_review_passed` will not let code-review exit without a
carriage saved after the latest sentinel, because a forgotten one would let the
item reach completion as DONE (a task mid-review at upgrade saves
`SPEC_DEFERRED: none`). `require_deferred_criteria_done` blocks the
documentation step until the PASSED note exists, is newer than the carriage and
matches the current tree; a log with no carriage at all passes there, for tasks
that left code-review before the feature existed.
Items the re-audit finds UNVERIFIABLE join a fresh `SPEC_UNVERIFIABLE:` note and
the work-log carry-list.

Each of these notes counts only in the step that owns it, read from the protocol
config: the sentinel and the carriage in the step that runs
`require_spec_review_passed`, `SPEC_DEFERRED: PASSED` in the step that runs
`require_deferred_criteria_done`. Review of this design found four ways a note
saved in the wrong step could satisfy or cancel a gate — a `none` saved during
documentation, a sentinel re-saved there, a confirmation saved during code
review — so the gates ignore notes from other steps instead of guessing which
note belongs to which audit.

On EVERY spec PASS the orchestrator records a companion note immediately after
the sentinel: `SPEC_UNVERIFIABLE: SC-x, SC-y`, `SPEC_UNVERIFIABLE: none`
(explicit none supersedes a stale earlier list), or
`SPEC_UNVERIFIABLE: legacy — see work log` for tasks without SC IDs (generated
positional labels would not survive re-review rounds). Non-empty sets ALSO
append a work-log carry-list — per item: SC-ID or verbatim criterion quote +
Verification Mode + why unverifiable + what the user should check — the
post-compaction mode source and the primary channel for legacy tasks. Because
the verdict is strict, "PASS + carriage note" is a *complete* per-SC status
record: everything outside the set is DONE by construction (task-completion.md
Section 1 states this reconstruction rule for post-compaction sessions).

## Per-Item Confirmation at Completion (§1a)

Applies to all four consumers of `task-completion.md` (task / refactoring /
optimize / optimize-unattended — all run the spec stage; only a custom fork
without it skips). BEFORE any generic confirmation — a global "LGTM" never
covers UNVERIFIABLE items:

- The set resolves through a five-case fail-closed chain: fresh `none` → skip;
  fresh SC-IDs → set + mandatory carry-list load (missing → §1b); fresh
  `legacy` → carry-list IS the set (missing → §1b); **stale note** (older than
  the latest sentinel — the staleness check catches a session dying between the
  two saves) → §1b directly, the superseded carry-list is never served; no note
  at all → carry-list fallback, else §1b.
- 1–3 items → one `AskUserQuestion` call, one question per item (quotes the
  criterion, names its mode, says what to check). >3 → a single call with
  `multiSelect`, options grouped ≤4 per question, ≤4 questions per call
  (= 16 items); >16 → successive calls. Widget limits are load-bearing here —
  the numbers come from the AskUserQuestion schema.
- A not-confirmed item → `protocol_goto` back to implementation, or an explicit
  per-item acceptance recorded in `## User Notes` + work log.

## Fail-Open Prohibitions (§1b + review side)

"The audit could not run" is never "the audit passed":

- **Review side** (`code-review.md` §6): a dispatch with no parseable verdict is
  NEITHER PASS NOR FAIL — one re-dispatch, then an explicit `AskUserQuestion`
  with **"Stop and fix"** first; the only alternative is `protocol_abort`
  (branch and work survive). There is deliberately NO proceed-without-audit
  path — `require_spec_review_passed` structurally blocks advance without the
  sentinel, and recording the sentinel without a real PASS is prohibited.
- **Completion side** (`task-completion.md` §1b): triggered by "cannot produce
  the per-SC status list", an errored (vs failed) verification command, or any
  §1a routing (missing/inconsistent/stale carriage data — even when other audit
  evidence exists). Distinction: ran-and-FAILED → fix loop; COULD-NOT-RUN → the
  explicit question ("proceed — this review pre-dates the carriage contract" is
  a knowing user answer for pre-feature runs, never a silent default).

## Gotchas

- The carriage contract is prompt-only (accepted wave-1 degradation): a
  forgotten note is caught by the fail-closed §1a chain, but engine-side pairing
  of the `SPEC_REVIEW:`/`SPEC_UNVERIFIABLE:` notes is Phase 2 work.
- The round-1 scope guard falsely excluded `refactoring` from §1a because a
  stale wiki-hub bullet claimed it had no spec stage — `refactoring.json` has
  carried `require_spec_review_passed` since m-hooks-hygiene-sweep. Verify
  protocol capabilities against the JSON, not prose.
- A fresh-but-unparseable carriage note is not a lettered §1a case — it falls
  under the audit-data-inconsistency umbrella (→ §1b), reachable only by
  violating the step-5 contract.

## Related

- [Review Output Contract](pages/entities/review-output-contract.md) — the code-quality half of the same review step.
- [Workflow Protocols](pages/protocols/workflow-protocols.md) — the two-stage gate this vocabulary plugs into.
- [Completion and Git Flow](pages/procedures/completion-and-git-flow.md) — the engine dispatch that runs after the confirmations.
- [Specialized Agents](pages/entities/specialized-agents.md) — the spec-compliance-reviewer agent entry.
