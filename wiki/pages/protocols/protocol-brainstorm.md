---
title: brainstorm Protocol
tags: [protocols, agents, ai-providers, daic]
created: 2026-05-31
updated: 2026-07-28
sources: [plugin/protocol-configs/brainstorm.json, plugin/protocol-configs/sub-protocols/brainstorm-topic.md, plugin/protocol-configs/sub-protocols/brainstorm-discussion.md, plugin/protocol-configs/sub-protocols/brainstorm-analysis.md, plugin/protocol-configs/sub-protocols/brainstorm-results.md, plugin/protocol-configs/sub-protocols/brainstorm-planning.md]
---

# brainstorm Protocol

`brainstorm` is the structured-ideation protocol: it takes a fuzzy "should we build X?" and turns it into a justified results document plus a set of concrete, coverage-audited implementation tasks — **without writing a line of production code**. Its centerpiece is a 6-specialist parallel analysis with a conflict-resolution loop. The config is `brainstorm.json` (5 steps, `brainstorm.json`): topic → discussion → analysis → results → planning. For the shared JSON step schema and how brainstorm relates to the other protocols see [Workflow Protocols](pages/protocols/workflow-protocols.md).

**Every step runs in `documentation` mode.** The whole protocol is non-implementing by design — the LLM edits the brainstorm task file and the results document but source edits are hook-blocked throughout. The product is knowledge and a plan, handed off to a separate `task` protocol run for the actual build. Task/branch convention: `b-brainstorm-<name>` on `brainstorm/<name>` (the `b-`→`brainstorm/` special branch prefix).

## Step 1 — topic (`mode: documentation`)

Define the topic, set initial scope, compose the brainstorm task file for automated creation.

- **`pre_funcs`**: `auto_detect_task`.
- **`post_funcs`** (`post_funcs_stop_on_failure: true`): `set_task_state`, `create_task_file`, `git_setup_branch`, `create_issue_if_enabled`, `update_task_status_in_progress` (`brainstorm.json`).
- **`advance_args`**: `task`, `branch`, `task_content`.

A brief, focused discussion (`brainstorm-topic.md`): **what** the topic is (one sentence), **why** it's being considered, **which** modules are affected — no deep dive (that's step 2). The composed task file ships with the scaffold the rest of the protocol fills in: `## Decisions`, `## Expert Analysis` (six empty subsections — Architecture, Code Impact, Critique, User Perspective, Risks & Security, Scope & Phasing), and a `## Conflicts` table. After explicit user confirmation, `protocol_advance(args={task, branch, task_content})` creates the task + branch + issue.

## Step 2 — discussion (`mode: documentation`) — also the conflict-resolution step

Deep discussion that records every accepted decision in the task file. This step does double duty: it is **re-entered from analysis** when experts conflict.

- No funcs; advance is gated by the `end` condition (all `[ ]` items resolved, user confirms readiness).

What the LLM does (`brainstorm-discussion.md`):
1. **Structured discussion** across functional requirements, boundaries, integration, users, constraints, priorities plus the cross-cutting ambiguity checklist (edge cases / failure modes, non-functional requirements, terminology / data definitions, integration points) — using the same Question Discipline as `task` investigation (TodoWrite backlog, Impact × Uncertainty prioritization with a max-5 widget-questions cap — remainder closed as reasonable defaults recorded under `## Decisions` — one `AskUserQuestion` per call with a `Recommendation: <option> — <reasoning>` line before every option-based widget, field-content discipline, output buffer). One option slot may be reserved for a "Not sure — let's discuss" choice that drops to plain prose.
2. **Propose 2-3 distinct approaches with trade-offs** for any non-trivial design decision; record the chosen approach **and** the rejected alternatives under `## Decisions` (preserving rejected options documents *why* the winner won).
3. **Record decisions immediately** with `[x]` (confirmed) / `[ ]` (still open). The order is **summarize → confirm with user → record** — once a decision is in `## Decisions`, downstream analysis treats it as load-bearing, so misinterpretations must be caught before it's committed.
4. **On conflict-resolution re-entry** (Section 3): read `## Conflicts`, present each conflict's competing viewpoints, let the user decide, record resolutions as new `[x]` decisions, and **clear every `[ ]` item** — analysis cannot re-run with open questions.

## Step 3 — analysis (`mode: documentation`) — 6 parallel specialists + the conflict loop

The defining step. It launches **6 specialist subagents** in a single parallel-dispatch message and, when configured, AI providers in the **same** message.

- **`pre_funcs`**: `resolve_ai_providers_for_brainstorm`.
- The `end` condition is unusually strict (`brainstorm.json`): *"You MUST launch specialist subagents every time this step is entered — including re-entries after conflict resolution. Do NOT skip agent launches or reuse previous results."*

The 6 specialists (`brainstorm-analysis.md`), each fed the brainstorm task file as context:

| # | Specialist | Dispatch | Focus |
|---|-----------|----------|-------|
| 1 | Architect | `subagent_type: "Plan"` + `code-architect.md` | system design, integration, feasibility, architectural risk |
| 2 | Code Impact | `subagent_type: "Explore"` + `code-explorer.md` | (A) reusable code with file:line refs, (B) deprecated/dead code |
| 3 | Critic | `general-purpose` + `critic.md` | devil's advocate, YAGNI, unnecessary complexity |
| 4 | User Perspective | `general-purpose` + `user-perspective.md` | end-user impact and UX |
| 5 | Risk & Security | `general-purpose` + `risk-security-analyst.md` | risks, dependencies, attack surface |
| 6 | Scope & Phasing | `general-purpose` + `scope-strategist.md` | scope definition, phasing, execution order |

When AI providers are configured, their `codex-cli`/`agy-cli` `Task` calls go into the **same single message**, giving **N+M concurrent agents** and weighting provider output equally with specialist output during conflict detection (`brainstorm-analysis.md`). See [AI Provider Integration](pages/subsystems/ai-provider-integration.md) and [Specialized Agents](pages/entities/specialized-agents.md).

**Result processing** (`brainstorm-analysis.md`):
1. Write each specialist's summary into its `## Expert Analysis` subsection.
2. Present a consolidated summary (agreements, notable findings, concerns).
3. **Conflict detection** — find fundamental disagreements (e.g. "Architect: new service" vs "Critic: extend existing").
4. **If conflicts** → document them in the `## Conflicts` table and `protocol_goto(step_name="discussion", reason="Resolve N conflicts")`. **If none** → advance to results.

The **re-run rule** is the whole point of the loop: even if the `## Expert Analysis` sections are already populated from a prior run, you MUST re-launch agents on re-entry — the old analysis was based on superseded decisions. Re-run at minimum the specialists whose domains the resolved conflicts touched; when in doubt, re-run all 6.

## Step 4 — results (`mode: documentation`)

Synthesize a clean results document and get user approval.

- No funcs; advance gated by the `end` condition.

The LLM writes `docs/brainstorm-results/<name>.md` (`brainstorm-results.md`) — a synthesis (not a copy) pulling from `## Expert Analysis`, `## Decisions`, `## Conflicts`. Required sections: Topic, Key Features, Justification, Scope Definition, Feature Details, Reusable Code (file:line), Deprecated / Dead Code (file:line). Then it presents **3 task-granularity options** as tables — Minimal (large consolidated tasks) / Balanced (moderate split) / Fine-grained (many small tasks) — and the user picks one, which becomes the `## Implementation Plan` section. Before advancing, a **Spec Self-Review Checklist** runs inline: placeholder scan, internal consistency, scope check, ambiguity check, and a **decision-coverage pre-check** (every `[x]` decision and `## Feature Details` entry maps to a planned task).

## Step 5 — planning (`mode: documentation`) — task creation + traceability audit

Create the implementation task files, prove nothing was dropped, then complete the protocol.

- **`pre_funcs`**: `present_completion_options`.
- **`post_funcs`** (`post_funcs_stop_on_failure: true`): `require_discard_confirmation`, `completion_dispatch`.

What the LLM does (`brainstorm-planning.md`):
1. **Create task files** per the approved Implementation Plan (standard `TEMPLATE.md` format, `h-`/`m-`/`l-` priority, `## Context Files` pointing at the results document, `## Dependencies` / `## Parallelizable With` for multi-task plans).
2. **Coverage Verification (Traceability Audit, Section 2)** — the rigorous gate. Enumerate every source item (confirmed `[x]` decisions, `## Feature Details` entries, in-scope items, `## Deprecated / Dead Code` entries, stray `[ ]` questions), then build a `## Task Coverage` matrix mapping each to a **created task file's** Success Criteria — or to an explicit `OUT OF SCOPE` / `DEFERRED → <task>` waiver. **No blank rows, no silent drops; do not advance below 100% covered-or-waived.** Critically, the results document's own `## Implementation Plan` does **not** count as coverage — that is the plan this audit checks *against*; only a real task file counts.
3. **Present the matrix** with a count ("N source items — X covered, Y waived, 0 unaccounted"), write it into the results document, and wait for user confirmation.

On advance, `completion_dispatch` runs the same routing as `task`'s completion step — provider-driven chain or the disabled-provider 4-option menu (`brainstorm-planning.md`). See [Completion and Git Flow](pages/procedures/completion-and-git-flow.md). The committed artifacts here are the results document + the new task files + the archived brainstorm task file (no production code).

## Design Decisions

- **Documentation mode throughout.** Brainstorm produces a plan, not code. Locking every step to documentation mode makes "no implementation during ideation" a hook-enforced invariant rather than a guideline.
- **Six fixed perspectives, dispatched in parallel.** A single agent's analysis has blind spots; six specialists with distinct mandates (build / reuse / criticize / user / risk / scope) surface disagreement that a monolithic pass would smooth over. Parallel dispatch makes the breadth cheap in wall-clock.
- **Conflict detection drives an explicit loop back to discussion.** Rather than letting the LLM silently reconcile expert disagreement, the protocol forces it to the user and re-runs analysis on the resolved decisions — the disagreement is treated as signal, not noise.
- **Re-entry must re-launch agents.** Reusing stale analysis after a decision change defeats the loop; the `end` condition forbids it explicitly.
- **The traceability audit is the Verification-Before-Completion gate applied to planning.** Task files are a lossy synthesis of decisions + features; the 100%-coverage matrix proves the lossiness didn't drop anything load-bearing before the brainstorm ships.

## Gotchas

- **brainstorm re-entry must re-launch the 6 specialists.** The single most-emphasized rule in `brainstorm-analysis.md` — populated `## Expert Analysis` sections are *not* a reason to skip the relaunch on conflict-resolution re-entry.
- **Coverage cannot cite the results document's Implementation Plan.** A row is "covered" only when it maps to a created task file's Success Criteria. Citing the plan being audited would pass an item that never reached a real task — the exact regression the audit exists to catch.
- **`## Scope Definition` is free prose, not a list.** The audit requires decomposing it into discrete in-scope rows, one each — collapsing it into one vague row defeats the audit.
- **The planning step routes through `completion_dispatch`, like `task`/`refactoring`/optimize.** The disabled-provider 4-option menu applies here. (Contrast [research](pages/protocols/protocol-research.md), whose conclusion uses the plain archive chain instead.)
- **Two checklists, two scopes.** The results step's Spec Self-Review is the lighter plan-level pre-check (decisions + features); the planning step's audit is the full check against the actual task files (adds in-scope items and dead-code entries). Don't conflate them.
