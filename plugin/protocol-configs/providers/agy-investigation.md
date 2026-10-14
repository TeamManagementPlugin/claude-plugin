# agy — Task Investigation

You are participating in **{phase}** for task `{task_name}` on branch `{branch}`.
Task file: `{task_file_path}`

## Context: Plan Summary
{plan_summary}

## Your job
Run `agy --add-dir "$PWD" --print-timeout 480s -p ...` (no permission flag; contained by the project read-only gate; analysis only — do NOT create or modify any files) against the repository and produce an independent reading of the task scope. Focus on what the main agent's investigation step is likely to miss: hidden coupling, prior decisions that constrain the design, unstated assumptions in the task file.

## Budget
You have 8 minutes, and your whole answer is lost if the deadline arrives mid-turn.
- The read-only gate denies test and build runners (pytest, npm, make, …) and any `;`, `&&`, `||`, redirection or command substitution — do not try them. It also denies any command containing `$`, parentheses, braces, `<` or `>`, even inside a quoted search pattern: search for a literal with `rg -F` / `grep -F`, or view the file range instead. Single read-only commands and pipelines of them work.
- Read the diff, if there is one, and the line ranges you need; do not page through large files end to end.
- Start writing your answer early, as soon as you have something useful to say — well before the deadline.

## Output shape
Markdown headings; one or two short bullets per section. Empty sections may be omitted.

## Plan Summary
The task in two sentences as you read it. Flag if your reading diverges from the task file's framing.

## Risks
Constraints / coupling / prior decisions the main agent's plan may overlook. Cite paths.

## Open Questions
Ambiguities that need user clarification before implementation begins.

## Verification
How an implementer should prove the investigation is complete — what file:line or behaviour would close the residual ambiguity.

If your reading matches the task file with no additions, return:
> Reading aligns with task file; no additional concerns.
