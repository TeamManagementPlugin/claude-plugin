---
title: research Protocol
tags: [protocols, daic, agents, ai-providers]
created: 2026-05-31
updated: 2026-05-31
sources: [plugin/protocol-configs/research.json, plugin/protocol-configs/sub-protocols/research-scoping.md, plugin/protocol-configs/sub-protocols/research-exploration.md, plugin/protocol-configs/sub-protocols/research-conclusion.md]
---

# research Protocol

`research` is the technical-investigation protocol: spikes, PoCs, architecture analysis, and technology evaluations. It produces a **knowledge artifact** (findings + recommendation) rather than shippable code — there is no commit/push/MR step and **no git branch**. The config is `research.json` (4 steps, `research.json`): scoping → exploration → synthesis → conclusion. For the shared JSON step schema and how research sits among the other protocols see [Workflow Protocols](pages/protocols/workflow-protocols.md).

Research is the protocol you reach for when you don't yet know enough to write a `task`. Three of its four steps are `discussion` mode (only synthesis is `documentation` mode); the only writes are to the research task file. Task convention: `r-<name>`, **`branch: none`** always.

## Step 1 — scoping (`mode: discussion`)

Define a clear, answerable research question; classify the type; bound the scope; compose the task file.

- **`pre_funcs`**: `auto_detect_task`.
- **`post_funcs`** (`post_funcs_stop_on_failure: true`): `set_task_state`, `create_task_file`, `create_issue_if_enabled`, `update_task_status_in_progress` (`research.json`). **Note the absence of `git_setup_branch`** — research creates no branch.
- **`advance_args`**: `task`, `task_content` (no `branch` arg).

What the LLM does (`research-scoping.md`):
1. **Formulate the research question** — specific, bounded, actionable.
2. **Classify the type** — one of `spike` (feasibility → PoC + findings), `architecture` (design analysis → ADR), `evaluation` (tool comparison → matrix + recommendation), `exploration` (codebase understanding → documented patterns).
3. **Set scope boundaries** — In Scope, Out of Scope (prevents creep), Time Box, Decision For.
4. **Compose the research task file** — a distinct shape from the standard template: `branch: none`, extra frontmatter (`research_type`, `time_box`, `decision_for`), `## Research Question` instead of `## Problem/Goal`, `## Scope` with In/Out subsections, and empty `## Findings` / `## Recommendation` (+`### Follow-Up Tasks`) sections.

After explicit user confirmation, `protocol_advance(args={task, task_content})` creates the task file and issue (if enabled) — no branch, no checkout.

## Step 2 — exploration (`mode: discussion`)

Deep, read-only investigation — gather evidence, analyze code, run read-only experiments, build understanding.

- **`pre_funcs`**: `resolve_ai_providers_for_exploration` (the func keeps the foundation-era `_for_exploration` name but reads the **`include_in_research_exploration`** config flag).
- No `post_funcs`; advance is gated only by the `end` condition (enough evidence to synthesize), and **no user confirmation is required** to advance out of this step — the user reviews at conclusion.

What the LLM does (`research-exploration.md`):
1. **Launch AI providers in parallel first** (Section 0), same pattern as elsewhere; findings under `## AI Provider Input — Research Exploration`. See [AI Provider Integration](pages/subsystems/ai-provider-integration.md).
2. **Investigate per research type** — the sub-protocol gives a tailored strategy for each of spike / architecture / evaluation / exploration (e.g. evaluation → define weighted criteria → research each candidate → build a comparison matrix). `code-explorer` agents (2-3 in parallel) and `context-gathering` are the heavy-lifting tools.
3. **Save notes obsessively** — `protocol_save_note()` after **every** significant finding/decision. This step's analysis is **not** auto-saved (only user messages survive compaction), so notes are the lifeline for session recovery.

**Boundary**: discussion mode blocks source edits. If exploration reveals the need for production changes, the contract is to *finish the research first then start a `task` protocol*, or `protocol_abort` and switch — never implement here (`research-exploration.md`).

## Step 3 — synthesis (`mode: documentation`)

Write the findings into the task file. This step's `start` text is **inline** in `research.json` (not an `@sub-protocol`), since it's short.

- No funcs; advance gated by the `end` condition.

Documentation mode (task-file + CLAUDE.md edits allowed, source blocked). The LLM populates `## Findings` with structured evidence, builds comparison matrices / decision tables for evaluations, documents trade-offs for architecture analyses, records what worked/didn't for spikes, writes the `## Recommendation`, lists `### Follow-Up Tasks` if any, and updates the work log.

## Step 4 — conclusion (`mode: discussion`)

Present findings, get explicit confirmation, archive.

- **`post_funcs`** (`post_funcs_stop_on_failure: false` — note the **false**): `archive_task`, `update_issue_status`, `cleanup_task_scoped_state`, `clear_task_state` (`research.json`).

What the LLM does (`research-conclusion.md`): present a concise summary (question, 3-5 key findings, recommendation, follow-up tasks), answer questions, optionally offer to create follow-up tasks, and wait for **explicit** confirmation. `protocol_goto(step_name="exploration")` reopens investigation; `protocol_goto(step_name="synthesis")` reopens the writeup. On advance, the engine archives the task file (with an optional lightweight commit to capture the doc in history), updates the issue, and cleans up state.

**`post_funcs_stop_on_failure: false` is deliberate**: if the optional archive commit fails (e.g. a git issue), cleanup still proceeds — the file *move* is the critical part, the commit is a convenience. This is why research uses the **plain archive chain, not `completion_dispatch`**.

## AI Provider Participation

Only the exploration step invites providers:

| Step | Resolver pre_func | Config flag |
|------|-------------------|-------------|
| exploration | `resolve_ai_providers_for_exploration` | `ai_providers.include_in_research_exploration` |

`include_in_research_exploration` was renamed from the legacy `include_in_exploration` (deprecated; values are not auto-forwarded). See [Configuration Schema](pages/entities/configuration-schema.md) and [AI Provider Integration](pages/subsystems/ai-provider-integration.md).

## Design Decisions

- **No branch, no git workflow.** Research output is knowledge, not code. Skipping branch creation and the commit/push/MR chain keeps research from polluting git with empty feature branches, and the `r-`/`branch: none` convention makes the no-branch state explicit in the task frontmatter.
- **Exploration advances without user confirmation.** Unlike most steps, the LLM can move from exploration to synthesis on its own — the quality gate is deferred to the conclusion step where the user reviews the written findings. This keeps a long investigation from stalling on a confirmation round-trip.
- **`post_funcs_stop_on_failure: false` on conclusion.** Archiving knowledge must not be held hostage by a flaky optional commit; the move is what matters.
- **Type classification drives strategy.** Tagging the research as spike/architecture/evaluation/exploration up front lets the exploration step prescribe a fit-for-purpose investigation method instead of one generic "go look around".

## Gotchas

- **research does NOT use `completion_dispatch`.** Its conclusion runs the plain `archive_task → update_issue_status → cleanup → clear` chain with `post_funcs_stop_on_failure: false`. The disabled-provider 4-option completion menu (`merge_local`/`push_pr`/`keep`/`discard`) **does not apply** to research — only [task](pages/protocols/protocol-task.md), [brainstorm](pages/protocols/protocol-brainstorm.md) (planning), [refactoring](pages/protocols/protocol-refactoring.md), and the optimize pair route through the dispatcher.
- **Save notes or lose your investigation.** The exploration step's analysis is not auto-persisted. A session compaction or restart with no `protocol_save_note()` calls loses everything but the user's own messages.
- **Don't implement during research.** The discussion-mode boundary is hook-enforced. The sanctioned path when you find you need code is "finish research, then start a `task`" — not editing source in exploration.
- **The completion path can fail on a missing branch if you mis-set frontmatter.** Research tasks deliberately have `branch: none`; the conclusion archive chain expects that. (For the standard task-completion behaviour where a missing branch *does* break the automated commit, see `task-completion.md`'s Limitations note.)
