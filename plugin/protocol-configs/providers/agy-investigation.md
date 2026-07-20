# agy — Task Investigation

You are participating in **{phase}** for task `{task_name}` on branch `{branch}`.
Task file: `{task_file_path}`

## Context: Plan Summary
{plan_summary}

## Your job
Run `agy --dangerously-skip-permissions -p ...` (contained by the project read-only gate; analysis only — do NOT create or modify any files) against the repository and produce an independent reading of the task scope. Focus on what the main agent's investigation step is likely to miss: hidden coupling, prior decisions that constrain the design, unstated assumptions in the task file.

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
