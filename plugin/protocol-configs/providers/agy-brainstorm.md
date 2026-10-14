# agy — Brainstorm Analysis

You are participating in **{phase}** for task `{task_name}` on branch `{branch}`.
Task file: `{task_file_path}`

## Context: Plan Summary
{plan_summary}

## Your job
Run `agy --add-dir "$PWD" --print-timeout 480s -p ...` (no permission flag; contained by the project read-only gate; analysis only — do NOT create or modify any files) and explore the codebase to surface concerns about the brainstorm topic that the in-house specialists (critic / user-perspective / risk-security / scope-strategist) may have missed. Independent perspective is the value — do not duplicate what the specialists likely cover.

## Budget
You have 8 minutes, and your whole answer is lost if the deadline arrives mid-turn.
- The read-only gate denies test and build runners (pytest, npm, make, …) and any `;`, `&&`, `||`, redirection or command substitution — do not try them. It also denies any command containing `$`, parentheses, braces, `<` or `>`, even inside a quoted search pattern: search for a literal with `rg -F` / `grep -F`, or view the file range instead. Single read-only commands and pipelines of them work.
- Read the diff, if there is one, and the line ranges you need; do not page through large files end to end.
- Start writing your answer early, as soon as you have something useful to say — well before the deadline.

## Output shape
Markdown headings; one or two short bullets per section. Empty sections may be omitted.

## Plan Summary
What is actually being proposed, in two sentences.

## Risks
The biggest single risk that would make this topic regret-worthy. Cite code (path:line) if you can.

## Open Questions
Questions the brainstorm hasn't surfaced yet. Phrase as questions, not assertions.

## Verification
How you would falsify the proposal — what experiment, log line, or test would catch it failing.

If you have nothing to add, return:
> No independent finding beyond what the specialist agents likely cover.
