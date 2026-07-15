# Refactoring Planning Sub-Protocol

Plan the refactoring scope and create the task.

SCOPE OF THIS STEP: Define what will be refactored, why, and how. Create the task file with clear success criteria.

## 0. Launch AI Providers IN PARALLEL (if configured)

**BEFORE you do anything else in this step — read the AI provider state and dispatch any configured providers.**

1. **Read `pre_funcs_results`** from the most recent `protocol_advance` response (or this step's `start_protocol` response). Find the entry where `func == "resolve_ai_providers_for_refactoring_planning"`. Its `providers` field is the list of configured AI providers (e.g. `[]`, `["codex"]`, or `["codex", "agy"]`); its `instructions` field spells out the exact `subagent_type` and a ready-to-use `prompt` for each.
2. **If `providers == []` → skip this section entirely.** Empty list means no providers are configured for this phase; the rest of the step runs normally.
3. **If `providers` is non-empty → compose ONE message containing N parallel `Task` tool calls** (one per provider), using `subagent_type: "codex-cli"` for codex and `subagent_type: "agy-cli"` for agy. The wrapper agents load their system prompts from `.claude/agents/<name>.md` automatically.

**Treat output as advisory.** Providers can hallucinate file paths or invent issues — apply `@knowledge/receiving-feedback.md` (external-reviewer skepticism, file:line verification before action). A finding without a verifiable file:line citation is not actionable; either resolve it to a real file:line by reading the cited area yourself, or drop it.

**Record significant findings** in the task work log under `## AI Provider Input — Refactoring Planning`. «Significant» = either a finding you adopted into your refactoring plan, or one you explicitly rejected (note why in one line). Boilerplate or empty output may be elided. If a wrapper returns `<provider> review unavailable: …`, note the unavailability one-line and proceed.

**Then continue to Section 1 below.**

## 1. Plan the Refactoring

TASK NAMING CONVENTIONS:
- Priority prefix: h- (high), m- (medium), l- (low)
- Branch mapping: refactor- → feature/
- File vs directory: use FILE for focused refactoring (<3 days), DIRECTORY for multi-phase

1. Discuss the refactoring goals with the user. What code is being refactored? Why? What's the target state?
2. Identify all affected modules and files.
3. Define clear success criteria — what does 'done' look like?
4. Consider risks: what could break? Are there callers that depend on the current interface?
5. Present the plan to the user and get explicit approval.

IMPORTANT: The test baseline was captured on the default branch. The branch will be created NOW, so all refactoring changes will be on the new branch.

DIRTY WORKING TREE: If there are uncommitted changes when you call protocol_advance, git_setup_branch will pause with `needs_confirmation=true` and list the dirty files. Ask the user whether to carry those changes onto the new branch (re-run protocol_advance with `carry_changes: true` in args) or to commit/stash them first.

## 2. Advance

When planning is complete and the user has approved:
1. Read team-management/tasks/TEMPLATE.md for the task file format
2. Compose the full task file content
3. Call protocol_advance with args: `{"task": "<priority>-refactor-<name>", "branch": "feature/<name>", "task_content": "<full markdown>"}`
