---
description: Analyze code for cleanliness issues (unused code, comment quality, formatting, naming, complexity). Delegates to the code-cleanliness agent.
argument-hint: "<path-or-glob> [--focus=category1,category2]"
---

# Code Cleanliness Check

Delegate a read-only cleanliness analysis of the target code to the `code-cleanliness` agent. The agent reports findings only — it never modifies files.

## Arguments

**$ARGUMENTS**:
- A file path, directory path, or glob pattern to analyze.
- Optional `--focus=` to limit analysis to specific categories (comma-separated).

## Usage

```
/team-management:clean-check src/utils.py
/team-management:clean-check src/**/*.ts
/team-management:clean-check lib/ --focus=unused,naming
/team-management:clean-check . --focus=comments,complexity
```

## Focus Categories

`unused`, `comments`, `formatting`, `naming`, `imports`, `duplication`, `magic`, `complexity`, `nesting`, `types`, `dead` — see the `code-cleanliness` agent for the definition of each.

---

**Invoke the `code-cleanliness` agent** via the Task tool (`subagent_type: "code-cleanliness"`) with:

> Target: $ARGUMENTS
>
> Analyze the specified code for cleanliness issues. Produce the standard report with severity classifications, exact file:line references, and actionable fix examples. If `--focus=` is specified, limit analysis to those categories only.
