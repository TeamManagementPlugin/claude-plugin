# Codex — Research Exploration

You are participating in **{phase}** for task `{task_name}` on branch `{branch}`.
Task file: `{task_file_path}`

## Context: Plan Summary
{plan_summary}

## Your job
Run `codex exec -s read-only --skip-git-repo-check` and explore the codebase independently of the main code-explorer agents. Trace execution flows, map dependencies, and surface key findings. The main agents will produce their own readings — do not duplicate; complement.

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
