# agy — Refactoring Planning

You are participating in **{phase}** for task `{task_name}` on branch `{branch}`.
Task file: `{task_file_path}`

## Context: Plan Summary
{plan_summary}

## Your job
Run `agy --add-dir "$PWD" --print-timeout 480s -p ...` (no permission flag; contained by the project read-only gate; analysis only — do NOT create or modify any files) and review the refactoring plan against the existing code. Refactoring's safety net is the test suite — your job is to flag refactor steps the existing tests do NOT cover, so the planner can either add a test first or pick a smaller increment.

## Budget
You have 8 minutes, and your whole answer is lost if the deadline arrives mid-turn.
- The read-only gate denies test and build runners (pytest, npm, make, …) and any `;`, `&&`, `||`, redirection or command substitution — do not try them. It also denies any command containing `$`, parentheses, braces, `<` or `>`, even inside a quoted search pattern: search for a literal with `rg -F` / `grep -F`, or view the file range instead. Single read-only commands and pipelines of them work.
- Read the diff, if there is one, and the line ranges you need; do not page through large files end to end.
- Start writing your answer early, as soon as you have something useful to say — well before the deadline.

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
