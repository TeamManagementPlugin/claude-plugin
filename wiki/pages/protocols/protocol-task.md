---
title: task Protocol
tags: [protocols, daic, agents, ai-providers, git]
created: 2026-05-31
updated: 2026-07-06
sources: [plugin/protocol-configs/task.json, plugin/protocol-configs/sub-protocols/task-investigation.md, plugin/protocol-configs/sub-protocols/task-implementation.md, plugin/protocol-configs/sub-protocols/code-review.md, plugin/protocol-configs/sub-protocols/task-completion.md, plugin/protocol-configs/sub-protocols/task-creation.md]
---

# task Protocol

`task` is the standard implementation lifecycle and the most-used protocol in the framework. It carries one unit of work from "understand the request" through to "merged, issue closed, task archived" across **5 steps**: investigation → implementation → code-review → documentation → completion. The config is `task.json` (5 steps, `task.json`); each step's verbose prompt lives in a separate `@sub-protocols/*.md` file resolved by the engine. For the shared JSON step schema, the `@`-ref resolution mechanism, and how `task` relates to the other five protocols, see [Workflow Protocols](pages/protocols/workflow-protocols.md). For per-step DAIC mode application see [DAIC Enforcement](pages/topics/daic-enforcement.md); for the git/issue side of completion see [Completion and Git Flow](pages/procedures/completion-and-git-flow.md).

The design intent: the LLM never flips DAIC mode by hand and never runs git/issue plumbing by hand. The engine applies the right mode on each step entry and runs the side-effecting funcs (`git_setup_branch`, `create_task_file`, `completion_dispatch`, …) automatically on `protocol_advance`. The LLM's job is to discuss, write code, and provide verification evidence; the protocol enforces the rest structurally.

## Step 1 — investigation (`mode: discussion`)

Read-only. The goal is to fully understand scope, align with the user on an approach, and compose the complete task-file content — **no code is written here** (`task-investigation.md`).

- **`pre_funcs`**: `auto_detect_task`, `resolve_ai_providers_for_investigation`.
- **`post_funcs`** (run on advance-out, `post_funcs_stop_on_failure: true`): `git_setup_branch`, `create_task_file`, `set_task_state`, `create_issue_if_enabled`, `update_task_status_in_progress` (`task.json`).
- **`advance_args`**: `task`, `branch`, `task_content` (`task.json`).

What the LLM does (`task-investigation.md`):
1. **Launch AI providers in parallel first** (Section 0) — if `resolve_ai_providers_for_investigation` returned a non-empty `providers` list, dispatch one `Task` per provider (`codex-cli` / `agy-cli`) before exploring. Output is advisory; significant findings recorded under `## AI Provider Input — Investigation`. See [AI Provider Integration](pages/subsystems/ai-provider-integration.md).
2. **Explore and ask** with the Question Discipline procedure: draft a `TodoWrite` backlog of clarifying questions across four dimensions (purpose, scope, affected modules, expected behaviour), then ask via `AskUserQuestion` **strictly one question per call** with the field-content discipline (the `question` carries 1-2 sentences of standalone context; each option `description` states the trade-off, not just the label).
3. **Propose 2-3 distinct approaches with trade-offs** — never present a single approach and proceed.
4. **Compose the Implementation Plan with the No-Placeholders rule**: exact paths and line ranges, full snippets, exact commands with expected output. `TBD`/`TODO`/"similar to Task N" are forbidden inside the plan.
5. **Plan self-review** before advancing: spec-coverage, placeholder scan, type-consistency.
6. **Alignment before advance**: present the summary, wait for **explicit** user agreement (silence ≠ agreement), read `TEMPLATE.md`, then deliver the task file. **Preferred for substantial files — write-file-first**: write the markdown to `team-management/tasks/<name>.md` (whitelisted in discussion mode) and advance with an **empty** `task_content` — the engine re-validates the on-disk file with strict parity and never overwrites it (see [Protocol Engine](pages/subsystems/protocol-engine.md)). Small files may pass full `task_content` inline. Large object-typed MCP args are unreliable (a multi-KB `task_content` can arrive empty / `__unparsedToolInput` above ~2KB), so emit the call **bare** and keep `args` small.

On advance the engine creates the task file, sets task state, creates + checks out the git branch, creates the provider issue (if enabled), and flips status to in-progress. A dirty working tree pauses `git_setup_branch` with `needs_confirmation=true` — re-advance with `carry_changes: true` to carry the changes onto the new branch (`task-investigation.md`). **Write-file-first triggers this pause by design**: `git_setup_branch` runs *before* `create_task_file`, so the pre-written task file (when `team-management/` is tracked) makes the tree dirty — that first advance pauses; re-advance with `carry_changes: true` to carry the task file onto the new branch. (`research` has no `git_setup_branch`, so its write-file-first path never pauses.) Task naming and branch mapping (`h-`/`m-`/`l-`/`r-`/`o-`/`b-` priority prefixes; `implement-`→`feature/`, `fix-`→`fix/`, `o-`→`optimize/`, `b-`→`brainstorm/`, `research-`→none) come from `task-creation.md`.

`investigation` is also the protocol's **re-planning anchor**: any later step that hits a broken assumption returns here via `protocol_goto(step_name="investigation", reason=...)`.

## Step 2 — implementation (`mode: implementation`)

Full edit access. Write code to satisfy every success criterion — no new scope, no drive-by refactors (`task-implementation.md`).

- **`pre_funcs`**: `verify_branch_and_task`, `resolve_ai_providers_for_implementation`.
- No `post_funcs`; advance is gated only by the step's `end` condition (all criteria checked, all tests pass, **changes not committed yet** — completion handles git).

What the LLM does:
1. **Launch AI providers in parallel** (Section 0), same pattern as investigation; findings under `## AI Provider Input — Implementation`.
2. **Critical plan review before writing any code** — re-read the Implementation Plan with fresh eyes; any gap (a step still needing a design decision, an ambiguous criterion, a changed assumption) triggers `protocol_goto(step_name="investigation")` rather than papering over it.
3. **Execute incrementally** — follow success criteria, check each off, prefer extending existing code over new abstractions, cover new code with tests per the TDD discipline (`team-management/knowledge/tdd-discipline.md`). Do **not** commit.
4. **Stop-on-Blocker** — halt and return to investigation on scope creep, broken assumption, 3-fix escalation, a test revealing an uncovered requirement, or a missing dependency.
5. **Advance-summary discipline** — the `summary` must evidence what was verified (commands + outputs), not predict it. The escape hatch `no-verification-applicable: <reason>` exists for structurally-unverifiable steps (canonical reasons only).

## Step 3 — code-review (`mode: implementation`) — the two-stage gate

This is the structurally-gated heart of the protocol. It runs in implementation mode (fixes can be applied in place) but cannot be advanced out of without passing two stages.

- **`pre_funcs`**: `resolve_ai_providers`, `require_spec_review_passed`.
- **`post_funcs`** (`post_funcs_stop_on_failure: true`): `require_spec_review_passed`, `verify_tests_pass`, `check_completion_evidence`, `validate_code_review_in_worklog`, `validate_no_critical_issues_in_worklog` (`task.json`).

### Stage 1 — spec compliance (FIRST, before code review)
The read-only `spec-compliance-reviewer` agent audits the git diff (working tree + staging) against the task's `## Success Criteria` and returns a PASS/FAIL verdict (`code-review.md`). On PASS, the orchestrator records the sentinel `SPEC_REVIEW: PASSED` **verbatim** via `protocol_save_note`. `require_spec_review_passed` runs as both a `pre_func` (entry `[BLOCKED]` reminder) and a `post_func` (hard block — advance is impossible without the sentinel). The post_func also invalidates a stale sentinel if a backward `protocol_goto` to a code-changing step happened after the sentinel's timestamp. **After any in-step code edit**, the PASS is stale and must be re-earned (re-dispatch the agent, re-save the sentinel) — a discipline rule the structural check does not yet enforce.

### Stage 2 — code quality (after the sentinel)
One message dispatches **N+1 parallel `Task` calls**: the mandatory Claude `code-review` agent plus one `codex-cli`/`agy-cli` per configured provider (`code-review.md`). Findings are aggregated with **equal weight**; provider output is advisory and never blocks. Every re-review round must repeat the full parallel dispatch (the sub-protocol calls out the anti-pattern of re-running only `code-review` "since the providers already ran"). The final passing review is appended to the work log in the exact `# Code Review: [Title]` format (single `#`) — this is what feeds the MR/PR review documentation.

### The advance gates
On `protocol_advance` out of code-review, four post_funcs must all pass:
- `require_spec_review_passed` — the sentinel exists and is not stale.
- `verify_tests_pass` — the **optional** test gate. Runs `config.json:test_command` under `subprocess.run(shell=False, timeout=600)` after a metacharacter scan + prefix allowlist; **skips gracefully** when `test_command` is null/empty/unreadable. Non-zero exit or timeout blocks advance. See [Configuration Schema](pages/entities/configuration-schema.md).
- `check_completion_evidence` — parses the advance `summary` and **rejects prose predictions**. It must contain a fenced output block, `N/N passed`, `exit 0`, counted check-marks, or the literal `no-verification-applicable: <reason>` escape hatch (`code-review.md`).
- `validate_code_review_in_worklog` / `validate_no_critical_issues_in_worklog` — the `# Code Review:` block is present and reports zero critical issues.

Warning enforcement (Section 5): in **strict** mode (`enforce_warnings=true`) the LLM fixes all warnings automatically and re-reviews until clean; in **relaxed** mode warnings are documented but don't block. Critical issues always block.

## Step 4 — documentation (`mode: documentation`, `skip_notification: true`)

Docs-only edits — source edits are **hook-blocked** here. If a code change turns out to be needed, the prompt mandates `protocol_goto(step_name="implementation")` rather than writing a TODO (`task.json`).

- **`pre_funcs`**: `wiki_update_reminder` — injects a prompt to update wiki pages when the LLM Wiki is enabled (see [LLM Wiki Feature](pages/subsystems/llm-wiki-feature.md)).
- The LLM runs the `service-documentation` agent (update every affected module's `CLAUDE.md`) and the `logging` agent (finalize the work log), then verifies the CLAUDE.md set is accurate. See [Specialized Agents](pages/entities/specialized-agents.md).
- **CLAUDE.md is kept lean, not accreted.** The step prompt (`task.json`) and the `service-documentation` agent both frame `CLAUDE.md` as a current-state reference, **not a changelog**: edit-in-place / replace-don't-append, prune superseded or duplicated content, and no task-by-task history or task-name tags (that history lives in git + the work log). The work log itself is exempt — step 4 keeps its full chronological record.

## Step 5 — completion (`mode: discussion`)

User verifies, then the engine does all git/issue/archive work automatically.

- **`pre_funcs`**: `present_completion_options`.
- **`post_funcs`** (`post_funcs_stop_on_failure: true`): `require_discard_confirmation`, `completion_dispatch`.

The sub-protocol (`task-completion.md`) requires: a **TDD self-acknowledgment** (was TDD applied or is the task exempt by a named category?), a change summary, a **mandatory `notify_user` call** (the user is likely not watching the terminal), and **explicit** confirmation before advancing. On advance, `completion_dispatch` branches on `issue_tracking.provider`:
- **Provider enabled** (`gitlab`/`github`/`jira`, plus legacy/unreadable configs) → the provider-driven chain: archive → commit → merge default → push → create MR/PR → update issue → cleanup → checkout default.
- **Provider `"disabled"`** → the 4-option menu (`merge_local` / `push_pr` / `keep` / `discard`); the user picks via `completion_option` in the advance args. `discard` is gated by the two-step typed confirmation (`require_discard_confirmation`).

Full dispatcher mechanics, branch-safety precondition, default-branch detection, the `gh pr` idempotency precheck, and the discard gate live in [Completion and Git Flow](pages/procedures/completion-and-git-flow.md).

## AI Provider Participation

Three of the five steps invite external AI providers, each gated by its own config flag (see [AI Provider Integration](pages/subsystems/ai-provider-integration.md)):

| Step | Resolver pre_func | Config flag |
|------|-------------------|-------------|
| investigation | `resolve_ai_providers_for_investigation` | `ai_providers.include_in_investigation` |
| implementation | `resolve_ai_providers_for_implementation` | `ai_providers.include_in_implementation` |
| code-review | `resolve_ai_providers` | `ai_providers.include_in_code_review` |

When a phase's flag is off (or no providers configured), the resolver returns `providers: []` and the corresponding Section 0 is skipped — the step runs single-agent (or, for code-review, Claude `code-review` alone).

## Design Decisions

- **Investigation is read-only and ends with explicit alignment.** Forcing discussion + a user-confirmed plan before any edit is the DAIC core — it prevents the over-implementation failure mode of unguided AI coding.
- **The branch/task/issue side effects are post_funcs, not LLM steps.** Setup is deterministic and atomic-ish (`post_funcs_stop_on_failure: true` halts the chain on the first failure), so a half-created task can't leave dangling state.
- **Code-review is two stages because each catches a different failure.** Stage 1 ("does the diff match the promise?") catches well-written code that drifted from the spec; Stage 2 ("is the diff correct/secure/consistent?") catches correctness bugs. Either alone misses the other class.
- **The sentinel + completion-evidence gates are un-bypassable by construction.** They're `post_funcs` with `post_funcs_stop_on_failure: true`, so they're hard preconditions for advancing, not advisory reminders.
- **Completion is the only place git runs.** Concentrating all VCS operations in one engine-driven step means the LLM never commits prematurely and the provider-vs-disabled routing has a single home.

## Gotchas

- **`task` uses the heavy two-stage review; `refactoring` does not.** Only `task` and the optimize pair carry `require_spec_review_passed` + `check_completion_evidence`. The [refactoring](pages/protocols/protocol-refactoring.md) code-review step is lighter (no spec-compliance sentinel). Don't assume the sentinel applies everywhere.
- **The sentinel goes stale on in-step edits, silently.** The structural staleness check only catches backward `protocol_goto`. If you fix code inside the code-review step after PASS, you must manually re-dispatch the spec-compliance agent and re-save the sentinel — nothing forces it.
- **Documentation mode blocks source edits, it doesn't merely discourage them.** An `Edit` to a `.py` file in step 4 is hook-blocked; the contract is `protocol_goto` back to implementation.
- **`check_completion_evidence` rejects "looks good".** The advance summary out of code-review must carry literal verification evidence or the canonical escape-hatch token. Prose predictions fail the gate.
- **The work-log severity headings are gate-parsed, and the two gates are NOT emoji-symmetric.** `validate_no_critical_issues_in_worklog` (`protocol_engine.py`) accepts `## Critical Issues (N)` with the 🔴 *optional*, but the warnings gate (`protocol_utils.check_code_review_warnings`, strict mode) *requires* the 🟡 in `## 🟡 Warnings (N)` — a plain `## Warnings (N)` is silently invisible to it. The Critical heading must also carry a concrete digit (`(0)` for "none found"); a literal `(N)` there fails **closed** and blocks completion (Warnings `(N)` merely fails open / non-enforcing). Both the `code-review` agent output template and the `sub-protocols/code-review.md` §4 work-log template emit these emoji headings; `test/test_code_review_heading_gates.py` drift-guards that both parse through both gates.
- **AI provider Section 0 must run *before* the rest of the step.** The providers explore concurrently with the LLM; dispatching them late wastes the parallelism the design is built around.
- **Author sub-protocol edits in `plugin/protocol-configs/sub-protocols/` (the package source), not the deployed `team-management/protocol-configs/system/` copy** — the latter is the protected legacy install copy.
