# Task Implementation Sub-Protocol

SCOPE OF THIS STEP: Write code to satisfy all success criteria. Implementation work only — no new scope, no drive-by refactors.

## 0. Launch AI Providers IN PARALLEL (if configured)

**BEFORE you do anything else in this step — read the AI provider state and dispatch any configured providers. They run in parallel with your plan review; their output becomes input to Section 1.**

1. **Read `pre_funcs_results`** from the most recent `protocol_advance` response. Find the entry where `func == "resolve_ai_providers_for_implementation"`. Its `providers` field is the list of configured AI providers (e.g. `[]`, `["codex"]`, or `["codex", "agy"]`); its `instructions` field spells out the exact `subagent_type` and a ready-to-use `prompt` for each.
2. **If `providers == []` → skip this section entirely.** Empty list means no providers are configured for this phase; proceed directly to Section 1.
3. **If `providers` is non-empty → compose ONE message containing N parallel `Task` tool calls** (one per provider), using `subagent_type: "codex-cli"` for codex and `subagent_type: "agy-cli"` for agy. The wrapper agents load their system prompts from `.claude/agents/<name>.md` automatically.

**Treat output as advisory.** Providers can hallucinate file paths or invent issues — apply `@knowledge/receiving-feedback.md` (external-reviewer skepticism, file:line verification before action). A finding without a verifiable file:line citation is not actionable; either resolve it to a real file:line by reading the cited area yourself, or drop it.

**Record significant findings** in the task work log under `## AI Provider Input — Implementation`. «Significant» = either a finding you adopted into the implementation plan (becomes a new TodoWrite item or modifies an existing success criterion), or one you explicitly rejected (note why in one line). Boilerplate or empty output may be elided. If a wrapper returns `<provider> review unavailable: …`, note the unavailability one-line and proceed.

**Then continue to Section 1.**

## 1. Critical Plan Review (before writing any code)

Re-read the `## Implementation Plan` (or success criteria) section of the task file with fresh eyes. Flag concerns before you start:

- **Is every step actionable now?** A step that still requires a design decision = gap.
- **Do success criteria leave ambiguity?** «Ensure X works» without a verification command = gap.
- **Has anything changed since investigation?** New dependency, edited file elsewhere, user clarification that invalidates a step = gap.

If you find ANY gaps → `protocol_goto(step_name="investigation", reason="<what's missing>")` and re-plan with the user. Do NOT paper over gaps mid-implementation.

If the plan is clean, convert the `## Implementation Plan` checkboxes directly into a TodoWrite list — that becomes your live execution tracker, and checking items off mirrors checking off the task-file boxes.

## 2. Execute Incrementally

1. Follow success criteria from the task file — check each off as complete.
2. Update the work log as you make significant progress.
3. Investigate existing patterns before writing new code — prefer extending existing code over creating new abstractions.
4. All new code must be covered by tests unless the task is TDD-exempt per `@knowledge/tdd-discipline.md`. All tests must pass.
5. Do NOT commit — the completion step handles git operations automatically.

## 3. Stop on Blocker — Don't Guess

Halt mid-implementation and return to investigation when ANY of these triggers fires:

- **Scope creep** — the work now exceeds the success criteria (fixing X turns out to require touching Y and Z).
- **Broken assumption** — a schema, API shape, or config key is different from what the plan assumed.
- **3-fix escalation** — three failed fix attempts on the same bug signal the architecture or premise is wrong. Consult `@knowledge/debugging.md`.
- **Test reveals uncovered requirement** — writing a test surfaces a case the plan did not cover.
- **Missing dependency** — a file / module / function the plan references does not exist.

Action: `protocol_goto(step_name="investigation", reason="<trigger>")`. Surfacing the blocker is the job — silently guessing and hoping is not.

## 4. Advisory: Debugging Discipline

If a bug arises mid-implementation, consult `@knowledge/debugging.md` BEFORE attempting fixes. The four-phase methodology (root-cause investigation → pattern analysis → hypothesis → verification) is faster than guess-and-check, especially under time pressure. Apply the 3-fix escalation rule.

## 5. Advance Summary Discipline

When calling `protocol_advance`, the `summary` must evidence what you verified, not what you assume. Concrete claims backed by commands and outputs — not predictions.

**Good summaries:**
- «All 17 success criteria checked. `pytest tests/unit/test_hooks.py -v` → 43 passed, 0 failed. Manual trigger of the feature on the dev server shows expected output.»
- «Success criteria 1-9 verified by tests. Criterion 10 (Windows PowerShell hook install): `no-verification-applicable: no-test-suite-exists` — manual smoke test on Linux only, no Windows CI available.»
- «Behavioral content change only; updated service CLAUDE.md and two sub-protocol markdown files. `no-verification-applicable: documentation-only`.»

**Bad summaries (red flags):**
- «Should work.» «Tests probably pass.» «Looks good.» — these are predictions, not verification. See the verification gate in `@knowledge/debugging.md`.

### Escape Hatch: `no-verification-applicable: <reason>`

For steps that structurally cannot be verified by a command, use this marker in the summary. Canonical reasons (use the token verbatim — suffixes are flagged as anomalous in the audit log):

- `no-verification-applicable: documentation-only`
- `no-verification-applicable: planning-step`
- `no-verification-applicable: discussion-step`
- `no-verification-applicable: no-test-suite-exists`

Anomalous reasons are flagged in the audit log and can be reviewed. The escape hatch is not a default — most implementation steps can and must be verified.

## 6. Context Compaction

Context compaction runs automatically at the configured threshold — no action needed from you. The PreCompact hook saves task / branch / protocol / DAIC state; post-compact restoration injects a session summary.

## Going Back

If the plan needs re-thinking, `protocol_goto(step_name="investigation")`. Call `protocol_current()` to see the full protocol overview and choose a target step.
