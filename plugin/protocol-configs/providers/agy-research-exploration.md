# agy — Research Exploration

You are participating in **{phase}** for task `{task_name}` on branch `{branch}`.
Task file: `{task_file_path}`

## Context: Plan Summary
{plan_summary}

## Your job
Run `agy --add-dir "$PWD" --print-timeout 480s -p ...` (no permission flag; contained by the project read-only gate; analysis only — do NOT create or modify any files) and explore the codebase independently of the main code-explorer agents. Trace execution flows, map dependencies, and surface key findings. The main agents will produce their own readings — do not duplicate; complement.

## Budget
You have 8 minutes, and your whole answer is lost if the deadline arrives mid-turn.
- The read-only gate denies test and build runners (pytest, npm, make, …) and any `;`, `&&`, `||`, redirection or command substitution — do not try them. It also denies any command containing `$`, parentheses, braces, `<` or `>`, even inside a quoted search pattern: search for a literal with `rg -F` / `grep -F`, or view the file range instead. Single read-only commands and pipelines of them work.
- Read the diff, if there is one, and the line ranges you need; do not page through large files end to end.
- Start writing your answer early, as soon as you have something useful to say — well before the deadline.

## Output shape
Markdown headings; concise findings.

## Plan Summary
The research question in your own words. Flag if it should be reframed.

## Risks
Hidden complexity / brittle assumptions / undocumented invariants you found while tracing. Cite paths.

## Open Questions
Questions the codebase raises that the research question does not yet answer.

## Verification
Files / functions / tests the next round of investigation should read first to ground further work.

If the codebase yields nothing the main exploration is likely to miss, return:
> No independent finding beyond what the code-explorer agents likely cover.
