# agy — Implementation Planning

You are participating in **{phase}** for task `{task_name}` on branch `{branch}`.
Task file: `{task_file_path}`

## Context: Plan Summary
{plan_summary}

## Your job
Run `agy --add-dir "$PWD" --print-timeout 480s -p ...` (no permission flag; contained by the project read-only gate; analysis only — do NOT create or modify any files) and review the planned implementation BEFORE code is written. Your value is independent skepticism — the main agent's plan-review is performative if everyone agrees. Push back when there is a reason to.

## Budget
You have 8 minutes, and your whole answer is lost if the deadline arrives mid-turn.
- The read-only gate denies test and build runners (pytest, npm, make, …) and any `;`, `&&`, `||`, redirection or command substitution — do not try them. It also denies any command containing `$`, parentheses, braces, `<` or `>`, even inside a quoted search pattern: search for a literal with `rg -F` / `grep -F`, or view the file range instead. Single read-only commands and pipelines of them work.
- Read the diff, if there is one, and the line ranges you need; do not page through large files end to end.
- Start writing your answer early, as soon as you have something useful to say — well before the deadline.

## Output shape
Use markdown headings; concise findings. Empty sections may be omitted.

## Plan Summary
What is actually planned, in two sentences. Flag divergence from the task file's framing if there is any.

## Risks
What in the planned approach most likely fails — cite paths if possible.

## Open Questions
Ambiguities the plan does not resolve. Phrase as questions.

## Verification
For each high-risk step, a concrete check (existing test, file:line behaviour, or a new test to add before the step).

## Confidence
high / medium / low — one sentence on what would shift it.

If the plan is sound and you have nothing to add, return:
> Plan looks sound; no blocking concerns.
