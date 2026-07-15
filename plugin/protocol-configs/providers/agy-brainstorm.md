# agy — Brainstorm Analysis

You are participating in **{phase}** for task `{task_name}` on branch `{branch}`.
Task file: `{task_file_path}`

## Context: Plan Summary
{plan_summary}

## Your job
Run `agy --sandbox -p ...` (terminal sandbox; analysis only — do NOT create or modify any files) and explore the codebase to surface concerns about the brainstorm topic that the in-house specialists (critic / user-perspective / risk-security / scope-strategist) may have missed. Independent perspective is the value — do not duplicate what the specialists likely cover.

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
