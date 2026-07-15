# Brainstorm Multi-Perspective Analysis Sub-Protocol

## Purpose

Launch 6 specialist subagents — PLUS any configured AI providers — in parallel to analyze the brainstorm topic from different perspectives. Process their results, identify conflicts, and either advance or loop back for conflict resolution.

## 0. AI Providers Join the Parallel Dispatch (if configured)

**Read the AI provider state BEFORE you compose the parallel-dispatch message — provider Task calls go into the SAME single message as the 6 specialists, so the N+M tool calls all execute concurrently.**

1. **Read `pre_funcs_results`** from the most recent `protocol_advance` response. Find the entry where `func == "resolve_ai_providers_for_brainstorm"`. Its `providers` field is the list of configured AI providers (e.g. `[]`, `["codex"]`, or `["codex", "agy"]`); its `instructions` field spells out the exact `subagent_type` and a ready-to-use `prompt` for each.
2. **If `providers == []` → launch only the 6 specialists** as described in `## Specialist Launch` below.
3. **If `providers` is non-empty → in your single parallel-dispatch message, include both the 6 specialist `Task` calls AND one `Task` per provider** (`subagent_type: "codex-cli"` for codex, `subagent_type: "agy-cli"` for agy). The wrapper agents load their system prompts from `.claude/agents/<name>.md` automatically.

**Treat provider output as advisory, weighted equally with specialist output during conflict detection.** Providers can hallucinate file paths or invent issues — apply `@knowledge/receiving-feedback.md` (external-reviewer skepticism, file:line verification before action).

**Record significant provider findings** in the task file under a new section `## AI Provider Input — Brainstorm Analysis` (sibling to `## Expert Analysis`). If a wrapper returns `<provider> review unavailable: …`, note the unavailability one-line and proceed with the specialists.

## Pre-Launch Preparation

Before launching subagents:
1. Read the current brainstorm task file to get the latest topic, decisions, and any previous analysis
2. Prepare a context summary that includes:
   - The brainstorm topic description
   - All confirmed decisions from `## Decisions`
   - Any previous expert analysis (if this is a re-run after conflict resolution)
   - Specific areas to focus on (if re-running only relevant specialists)

## Specialist Launch

Launch subagents in parallel. Each specialist is a **registered subagent type** — dispatch it directly via `subagent_type`; its system prompt and allowed tools load automatically from its registration. Do NOT read or paste `.claude/agents/*.md` contents into the `prompt` — on a plugin install those files are not present in the project. If AI providers are configured (Section 0 above), include their `Task` calls in the SAME parallel-dispatch message so all N+M agents execute concurrently.

### Re-run Strategy

**First run**: Launch all 6 specialists.

**Re-run after conflict resolution**: You MUST re-launch agents — do NOT skip this step or reuse previous results. Resolved conflicts change the decision landscape and require fresh analysis.
- Minimum: re-run specialists whose domains were directly affected by the resolved conflicts
- If conflicts touched architecture/design → re-run Architect + Code Impact
- If conflicts touched scope/features → re-run Scope Strategist, User Perspective
- If conflicts touched risk/security → re-run Risk & Security Analyst
- If conflicts were broad or fundamental → re-run all 6
- When in doubt, re-run all for consistency

**CRITICAL**: Even if ## Expert Analysis sections are already populated from a previous run, you MUST launch agents again. Previous analysis was based on outdated decisions — the whole point of the loop is to get fresh perspectives after conflict resolution. Overwrite the old analysis with new results.

### Handling Agent Failures

If one or more agents fail (timeout, context overflow, error):
- Document which agents failed and why
- Present successful results to the user
- Ask whether to retry failed agents or proceed with partial results
- Do NOT block the entire analysis on a single agent failure

### 6 Specialist Subagents

**1. Architect** (reuse existing agent)
```
Agent tool:
  subagent_type: "code-architect"
  prompt: "BRAINSTORM ANALYSIS MODE: You are analyzing a brainstorm topic, not designing an implementation.
  Focus on: system design implications, integration patterns, technical feasibility, architectural risks.

  Topic and decisions:
  <brainstorm task file content>"
```

**2. Code Impact Analyst** (reuse existing agent)
```
Agent tool:
  subagent_type: "code-explorer"
  prompt: "BRAINSTORM ANALYSIS MODE — CODE IMPACT FOCUS:
  Analyze the codebase impact of this topic in TWO areas:

  A) CODE REUSE: Find all existing code, patterns, utilities, and abstractions that can be reused.
  Provide specific file:line references for each reusable component.
  Estimate how much can be built on existing foundations.

  B) DEPRECATION: Identify all existing code that will become unnecessary, deprecated, or dead code.
  Provide specific file:line references for each affected component.
  Categorize: fully obsolete, partially obsolete, needs modification.

  Topic and decisions:
  <brainstorm task file content>"
```

**3. Critic** (dedicated agent)
```
Agent tool:
  subagent_type: "critic"
  prompt: "Topic and decisions:
  <brainstorm task file content>"
```

**4. User Perspective** (dedicated agent)
```
Agent tool:
  subagent_type: "user-perspective"
  prompt: "Topic and decisions:
  <brainstorm task file content>"
```

**5. Risk & Security Analyst** (dedicated agent)
```
Agent tool:
  subagent_type: "risk-security-analyst"
  prompt: "Topic and decisions:
  <brainstorm task file content>"
```

**6. Scope & Phasing Strategist** (dedicated agent)
```
Agent tool:
  subagent_type: "scope-strategist"
  prompt: "Topic and decisions:
  <brainstorm task file content>"
```

## Result Processing

After all subagents complete:

### 1. Document Results
For each specialist, write a concise summary into the corresponding `## Expert Analysis` subsection in the task file:
- `### Architecture` — from Architect
- `### Code Impact` — from Code Impact Analyst (include file:line refs for both reuse and deprecation)
- `### Critique` — from Critic
- `### User Perspective` — from User Perspective
- `### Risks & Security` — from Risk & Security Analyst
- `### Scope & Phasing` — from Scope Strategist

### 2. Present Results to User
Provide a consolidated summary to the user highlighting:
- Key agreements across specialists
- Notable findings from each perspective
- Any concerns raised

### 3. Conflict Detection
Identify conflicts where specialists disagree on fundamental points. Examples:
- Architect says "add new service" but Critic says "too complex, extend existing"
- User Perspective says "critical feature" but Risk & Security Analyst says "high risk, defer"
- Scope Strategist plans component X but Critic argues it's unnecessary

### 4. Conflict Handling

**If conflicts found:**
1. Document each conflict in the task file's `## Conflicts` table:
   ```markdown
   | Architect vs Critic | Architect: new microservice | Critic: extend existing monolith | TBD |
   ```
2. Present conflicts to the user with both viewpoints
3. Call `protocol_goto(step_name="discussion", reason="Resolve N conflicts from expert analysis")`
4. The discussion step will handle resolution with the user

**If no conflicts:**
1. Confirm all results are documented in the task file
2. Present the consolidated analysis to the user
3. Advance to the results step

## Important Notes

- Documentation mode is active — you can edit the task file but not source code
- Save notes via `protocol_save_note()` after documenting results (session recovery)
- Do NOT advance if conflicts exist — always goto discussion first
- The user must confirm before advancing (either that conflicts are resolved or that analysis is satisfactory)
