---
title: refactoring Protocol
tags: [protocols, testing, daic, git, ai-providers]
created: 2026-05-31
updated: 2026-06-10
sources: [plugin/protocol-configs/refactoring.json, plugin/protocol-configs/sub-protocols/refactoring-planning.md, plugin/protocol-configs/sub-protocols/refactoring-incremental.md, plugin/protocol-configs/sub-protocols/refactoring-verify.md, plugin/protocol-configs/sub-protocols/code-review.md, plugin/protocol-configs/sub-protocols/task-completion.md]
---

# refactoring Protocol

`refactoring` is the test-baseline-gated restructuring protocol: change the shape of the code without changing its behaviour, with a test suite as the safety net at both ends. The defining feature is that it **captures a test baseline on the default branch before any change** and **formally compares against it** after, so a regression cannot slip through unnoticed. The config is `refactoring.json` (6 steps, `refactoring.json`): test-baseline → planning → refactoring → test-verify → code-review → completion. For the shared JSON step schema and the protocol family overview see [Workflow Protocols](pages/protocols/workflow-protocols.md).

The baseline-first ordering is the whole idea: step 1 runs **before** the branch exists (on `main`/`master`), so the baseline reflects the true starting point, not a state already perturbed by the refactor. Task convention: `<priority>-refactor-<name>` on `feature/<name>`.

## Step 1 — test-baseline (`mode: discussion`)

Run the test suite on the **default branch** and snapshot the result before any change.

- **`pre_funcs`**: `auto_detect_task`.
- **`post_funcs`** (`post_funcs_stop_on_failure: true`): `capture_test_baseline`.
- **`advance_args`**: `test_command`, `baseline_summary` (`refactoring.json`).

The step text is inline in `refactoring.json`. The LLM identifies the test command, runs the suite, and records results. **Pre-existing failures are acceptable** — they're stored in the baseline so they aren't later mistaken for regressions. If there is **no test suite**, the LLM considers whether tests should be written first; if the refactor is safe without them, it passes `test_command="none"` and `baseline_summary="No test suite found"`. Advance with `args={test_command, baseline_summary}`; `capture_test_baseline` persists the snapshot.

## Step 2 — planning (`mode: discussion`)

Define the refactoring scope, create the task, set up the branch.

- **`pre_funcs`**: `resolve_ai_providers_for_refactoring_planning`.
- **`post_funcs`** (`post_funcs_stop_on_failure: true`): `git_setup_branch`, `create_task_file`, `set_task_state`, `create_issue_if_enabled`, `update_task_status_in_progress`.
- **`advance_args`**: `task`, `branch`, `task_content`.

What the LLM does (`refactoring-planning.md`):
1. **Launch AI providers in parallel first** (Section 0); findings under `## AI Provider Input — Refactoring Planning`. See [AI Provider Integration](pages/subsystems/ai-provider-integration.md).
2. **Plan the refactor** — what code, why, target state, all affected files, clear success criteria, and a risk pass (what could break? which callers depend on the current interface?). Present and get explicit approval.

**The branch is created NOW, in this step** — so the baseline (captured on the default branch in step 1) and the refactoring work (on the new `feature/` branch) are cleanly separated. A dirty working tree pauses `git_setup_branch` with `needs_confirmation=true` (re-advance with `carry_changes: true`).

## Step 3 — refactoring (`mode: implementation`)

Incremental change with mandatory inter-increment testing.

- **`pre_funcs`**: `verify_branch_and_task`.
- No `post_funcs`; advance gated by the `end` condition (all increments complete, each tested and passing).

The sub-protocol (`refactoring-incremental.md`) opens with a **mandatory READ** of `@knowledge/tdd-discipline.md` and a hard precondition: *refactoring without a test safety net is untracked rewriting* — if the code has no tests, `protocol_abort` and open a separate test-authoring task first (the two discussion-mode steps before this one cannot write tests). Then a **per-increment RED-GREEN-REFACTOR checklist**:

1. **Baseline GREEN** — the baseline test command passes *before* you touch the increment.
2. **One logical change** — a single transformation (rename / extract / move / simplify / swap implementation behind a stable interface). Never bundle.
3. **GREEN** — re-run the test command; all pass.
4. **No new warnings** — compiler/linter output pristine.
5. **Save note** — `protocol_save_note()` with what changed + "tests passed".

If GREEN fails, fix immediately before the next increment — **never accumulate broken state**. The Scope Guard routes unrelated bugs and new-functionality urges to follow-up tasks, and significant scope growth back to planning via `protocol_goto`.

## Step 4 — test-verify (`mode: discussion`)

Formal regression gate against the baseline.

- **`pre_funcs`**: `load_test_baseline` — makes `test_command`, `baseline_summary`, `captured_on_branch` available in `pre_funcs_results`.
- No `post_funcs`; advance gated by the `end` condition (no regressions).

What the LLM does (`refactoring-verify.md`): run the **exact same** test command from the baseline and compare. **All previously-passing tests must still pass** (the core safety guarantee); pre-existing failures carried forward are fine; **new failures are regressions and must be fixed**; new passes are a noted bonus. On any regression → `protocol_goto(step_name="refactoring")`, fix, return here. Flaky-test handling (run 2-3×) and the `test_command: "none"` case (verify by other means, document the approach) are covered explicitly.

## Step 5 — code-review (`mode: implementation`)

Run the code-review agent + AI providers, fix all issues.

- **`pre_funcs`**: `resolve_ai_providers`, `require_spec_review_passed`.
- **`post_funcs`** (`post_funcs_stop_on_failure: true`): `require_spec_review_passed`, `check_completion_evidence`, `validate_code_review_in_worklog`, `validate_no_critical_issues_in_worklog` (`refactoring.json`).

This uses the **same `code-review.md` sub-protocol** as `task` — the N+1 parallel dispatch of the Claude `code-review` agent plus one `Task` per configured provider, equal-weight aggregation, the `# Code Review: [Title]` work-log block. Since m-hooks-hygiene-sweep (audit M2) the func wiring matches `task`'s two-stage gate — `SPEC_REVIEW: PASSED` sentinel + structural evidence check — with **one deliberate omission: `verify_tests_pass`**. That gate requires a clean exit 0 from `test_command`, but the refactoring protocol tolerates pre-existing baseline failures by design (test-verify diffs against the captured baseline); on a dirty-baseline codebase the exit-0 gate would block advance permanently. The omission is pinned by `test/test_refactoring_gates_drift.py`, which fails if `verify_tests_pass` is ever reintroduced so the trade-off gets re-litigated rather than silently reverted. Warning enforcement (strict/relaxed) still applies.

## Step 6 — completion (`mode: discussion`)

User verifies; engine does git/issue/archive; the test baseline is cleaned up last.

- **`pre_funcs`**: `present_completion_options`.
- **`post_funcs`** (`post_funcs_stop_on_failure: true`): `require_discard_confirmation`, `completion_dispatch`, **`cleanup_test_baseline`** (`refactoring.json`).

Uses the shared `task-completion.md` sub-protocol and the standard `completion_dispatch` routing (provider-driven chain or disabled-provider 4-option menu). The only structural difference from `task`'s completion is the appended `cleanup_test_baseline` — placed **after** `completion_dispatch` so the baseline is **preserved if dispatch fails** (you can retry without re-capturing). See [Completion and Git Flow](pages/procedures/completion-and-git-flow.md).

## AI Provider Participation

| Step | Resolver pre_func | Config flag |
|------|-------------------|-------------|
| planning | `resolve_ai_providers_for_refactoring_planning` | `ai_providers.include_in_refactoring_planning` |
| code-review | `resolve_ai_providers` | `ai_providers.include_in_code_review` |

## Design Decisions

- **Baseline before branch.** Capturing the test result on the default branch *before* the refactor branch exists is what makes the comparison trustworthy — there's no chance the "before" snapshot is already contaminated by the change.
- **Two test gates bracket the work.** test-baseline (step 1) and test-verify (step 4) turn "did I break anything?" from a judgement call into a mechanical diff against a recorded oracle. Pre-existing failures are tracked so they don't masquerade as regressions.
- **Per-increment testing localizes breakage.** Running the suite after each single transformation means a failure points at exactly one increment, not a tangle of bundled changes.
- **code-review gates aligned with `task`, minus `verify_tests_pass`.** Originally the wiring was lighter (no sentinel, no evidence gate) on the theory that the test baseline made them redundant — the framework audit (M2) rejected that: spec compliance and evidence discipline guard against failure modes the baseline cannot see (well-tested code that doesn't match the spec; unverified claims). Only `verify_tests_pass` stays out, because its exit-0 contract genuinely conflicts with the baseline-diff design (pre-existing failures are legal in refactoring). Drift-guarded by `test/test_refactoring_gates_drift.py`.
- **`cleanup_test_baseline` runs last and only on success.** Ordering it after `completion_dispatch` means a failed completion leaves the baseline intact for a retry.

## Gotchas

- **No test suite → abort and write tests first.** The protocol cannot manufacture a safety net for you. Its two pre-refactoring steps are discussion-mode and can't author tests; `refactoring-incremental.md` tells you to `protocol_abort` and open a test-authoring task when coverage is absent.
- **refactoring's code-review is lighter than task's — no `SPEC_REVIEW: PASSED`.** If you expect the spec-compliance sentinel here, you won't find it. The two-stage gate is task/optimize-only.
- **Regressions loop you back to step 3, not forward.** test-verify is a hard gate: any newly-failing test sends you to `protocol_goto(step_name="refactoring")` to fix before you can reach code-review.
- **`test_command: "none"` weakens both gates.** With no suite, the baseline and verify steps degrade to "verify by other means and document it" — the protocol still runs, but the safety net is gone. Prefer writing tests first.
- **Don't fold unrelated fixes into a refactor increment.** The Scope Guard is explicit: unrelated bugs and new functionality become follow-up tasks, not bundled commits.
