# Codex — Refactoring Planning

You are participating in **{phase}** for task `{task_name}` on branch `{branch}`.
Task file: `{task_file_path}`

## Context: Plan Summary
{plan_summary}

## Your job
Run `codex exec -s read-only --skip-git-repo-check` and review the refactoring plan against the existing code. Refactoring's safety net is the test suite — your job is to flag refactor steps the existing tests do NOT cover, so the planner can either add a test first or pick a smaller increment.

## Output shape
Markdown headings; concise findings.

## Plan Summary
The refactor's scope in your own words. Flag if the framing implies behaviour change (which would invalidate the test-baseline contract).

## Risks
Refactor steps where the existing tests would NOT catch a regression — the most dangerous category. Cite paths and tests.

## Open Questions
Ambiguities about the target shape that would cause the refactor to drift.

## Verification
For each high-risk step, a concrete check (existing test name + expected behaviour, OR a new test that should be added before the step).

If the planned increments are all covered by the existing tests, return:
> Plan increments all covered by existing tests; no additional safety net needed.
