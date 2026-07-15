# Research Scoping Sub-Protocol

## Purpose

Define the research question, classify the research type, set scope boundaries, and compose the research task file content for automated creation.

## 1. Define the Research Question

Work with the user to formulate a clear, answerable research question. Good research questions are:
- **Specific** — not "investigate caching" but "which caching strategy best fits our read-heavy API with 10k RPM?"
- **Bounded** — has clear success criteria for when research is "done"
- **Actionable** — the answer leads to a concrete decision or follow-up task

## 2. Classify Research Type

Determine which category this research falls into:

| Type | Purpose | Typical Output |
|------|---------|----------------|
| `spike` | Technical feasibility exploration | Working PoC + findings |
| `architecture` | System design analysis | Architecture decision record |
| `evaluation` | Technology/tool comparison | Comparison matrix + recommendation |
| `exploration` | Codebase understanding | Documented patterns + knowledge |

## 3. Set Scope Boundaries

Define explicitly:
- **In scope**: What will be investigated
- **Out of scope**: What will NOT be investigated (prevents scope creep)
- **Time box**: Suggested maximum time investment (e.g., "2 hours", "1 session")
- **Decision for**: What decision this research informs (if applicable)

## 4. Compose Research Task File

Research task files differ from standard task files:

```markdown
---
task: r-<descriptive-name>
branch: none
status: pending
created: YYYY-MM-DD
modules: [list of modules to investigate]
research_type: spike|architecture|evaluation|exploration
time_box: <duration estimate>
decision_for: <what decision this informs, or "general knowledge">
---

# [Research Title]

## Research Question
[Clear, specific question to answer]

## Scope

### In Scope
- [What will be investigated]

### Out of Scope
- [What will NOT be investigated]

## Success Criteria
- [ ] Research question answered with evidence
- [ ] Findings documented with supporting data
- [ ] Recommendation provided (if applicable)
- [ ] Follow-up tasks identified (if any)

## Findings
<!-- Populated during synthesis step -->

## Recommendation
<!-- Populated during synthesis step -->

### Follow-Up Tasks
<!-- List any implementation tasks that should be created -->

## Work Log
- [YYYY-MM-DD] Research scoped and task created
```

**Key differences from standard TEMPLATE.md:**
- `branch: none` always (research tasks don't create git branches)
- Task name prefix: `r-` (research/investigate priority)
- Additional frontmatter: `research_type`, `time_box`, `decision_for`
- `## Research Question` replaces `## Problem/Goal`
- `## Scope` with In/Out scope subsections
- `## Findings` section (empty until synthesis)
- `## Recommendation` with `### Follow-Up Tasks`

## 5. User Confirmation

Before advancing:
1. Present the research question, type, scope, and time box to the user
2. Wait for explicit agreement (e.g., "looks good", "go ahead", "approved")
3. Do NOT call protocol_advance until the user confirms

## When Ready

**Preferred for any substantial research file — write-file-first:** write the full markdown to `team-management/tasks/r-<name>.md` with the Write tool (whitelisted in discussion mode), then advance with an **empty** `task_content`: `protocol_advance(args={"task": "r-<name>", "task_content": ""})`. The engine re-validates the on-disk file and keeps it exactly as written. Large object-typed args are unreliable — a multi-KB `task_content` can arrive empty — so emit the call **bare** and keep `args` small.

Only for a genuinely small research file, pass the markdown inline. Call `protocol_advance` with args:
```json
{
    "task": "r-<name>",
    "task_content": "<full markdown content>"
}
```

Note: No `branch` arg — research tasks use `branch: none` and skip git branch creation.

SESSION RECOVERY: Call `protocol_save_note()` after defining the research question and scope. These notes survive context compaction and session restarts.
