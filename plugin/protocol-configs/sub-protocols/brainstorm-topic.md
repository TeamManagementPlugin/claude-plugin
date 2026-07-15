# Brainstorm Topic Definition Sub-Protocol

## Purpose

Define the brainstorm topic, set initial scope, and compose the brainstorm task file for automated creation with branch setup.

## 1. Brief Topic Discussion

Work with the user to clearly articulate what is being brainstormed. Keep this step focused and concise:
- **What** is the topic? (one sentence)
- **Why** is this being considered? (motivation)
- **Which modules/areas** are potentially affected?

Do NOT deep-dive into details — that happens in the next step.

## 2. Name the Brainstorm

Choose a descriptive name that follows the convention:
- Task name: `b-brainstorm-<descriptive-name>` (e.g., `b-brainstorm-plugin-system`)
- Branch name: `brainstorm/<descriptive-name>` (e.g., `brainstorm/plugin-system`)

## 3. Compose Brainstorm Task File

```markdown
---
task: b-brainstorm-<name>
branch: brainstorm/<name>
status: pending
created: YYYY-MM-DD
modules: [list of affected modules]
---

# [Brainstorm Title]

## Topic
[Brief description of what is being brainstormed and why]

## Success Criteria
<!-- What "done" looks like for this brainstorm -->
- [ ] Topic analyzed from all six specialist perspectives (Architecture, Code Impact, Critique, User Perspective, Risks & Security, Scope & Phasing)
- [ ] All key decisions recorded in ## Decisions and any expert conflicts resolved
- [ ] Results document written to docs/brainstorm-results/<name>.md
- [ ] Implementation tasks planned with full coverage of recorded decisions

## Decisions
<!-- Each decision will be recorded during discussion step -->

## Expert Analysis
<!-- Sections below are populated automatically during the analysis step (step 3) -->

### Architecture
### Code Impact
### Critique
### User Perspective
### Risks & Security
### Scope & Phasing

## Conflicts
<!-- Conflicting opinions between experts with resolutions -->
| Conflict | Expert A | Expert B | Resolution |
|----------|----------|----------|------------|

## Work Log
- [YYYY-MM-DD] Brainstorm scoped and task created
```

## 4. User Confirmation

Before advancing:
1. Present the topic, name, and affected modules to the user
2. Wait for explicit agreement (e.g., "looks good", "go ahead", "approved")
3. Do NOT call protocol_advance until the user confirms

**To abandon the brainstorm at any point**: call `protocol_abort(reason="...")`. This cleans up state without archiving.

## When Ready

Call `protocol_advance` with args:
```json
{
    "task": "b-brainstorm-<name>",
    "branch": "brainstorm/<name>",
    "task_content": "<full markdown content>"
}
```

DIRTY WORKING TREE: If there are uncommitted changes when you call `protocol_advance`, `git_setup_branch` pauses with `needs_confirmation=true` and lists the dirty files. Ask the user whether to carry those changes onto the new branch (re-run `protocol_advance` with `carry_changes: true` in args) or to commit/stash them first.

SESSION RECOVERY: Call `protocol_save_note()` after defining the topic and name.
