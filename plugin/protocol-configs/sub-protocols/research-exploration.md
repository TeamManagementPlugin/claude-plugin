# Research Exploration Sub-Protocol

## 0. Launch AI Providers IN PARALLEL (if configured)

**BEFORE you start your own exploration — read the AI provider state and dispatch any configured providers. They explore the codebase concurrently with you and surface evidence you might miss.**

1. **Read `pre_funcs_results`** from the most recent `protocol_advance` response. Find the entry where `func == "resolve_ai_providers_for_exploration"` (the func name keeps the foundation-era `_for_exploration` form; the config flag it consults is `include_in_research_exploration`). Its `providers` field is the list of configured AI providers (e.g. `[]`, `["codex"]`, or `["codex", "agy"]`); its `instructions` field spells out the exact `subagent_type` and a ready-to-use `prompt` for each.
2. **If `providers == []` → skip this section entirely.** Empty list means no providers are configured for this phase; proceed to «Purpose» / «Investigation Strategies» below.
3. **If `providers` is non-empty → compose ONE message containing N parallel `Task` tool calls** (one per provider), using `subagent_type: "codex-cli"` for codex and `subagent_type: "agy-cli"` for agy. The wrapper agents load their system prompts from `.claude/agents/<name>.md` automatically.

**Treat output as advisory.** Providers can hallucinate file paths or invent issues — apply `@knowledge/receiving-feedback.md` (external-reviewer skepticism, file:line verification before action). A finding without a verifiable file:line citation is not actionable; either resolve it to a real file:line by reading the cited area yourself, or drop it.

**Record significant findings** in the task work log under `## AI Provider Input — Research Exploration`. «Significant» = either evidence you adopted into the research synthesis, or one you explicitly rejected (note why in one line). Boilerplate or empty output may be elided. If a wrapper returns `<provider> review unavailable: …`, note the unavailability one-line and proceed.

**Then continue to «Purpose» / «Investigation Strategies» below.**

## Purpose

Deep investigation phase where you gather evidence, analyze code, run experiments, and build understanding. This step is in discussion mode — you can read code and run read-only commands but cannot edit production code.

## Investigation Strategies by Research Type

### Spike (Technical Feasibility)
1. Identify the specific technical question to answer
2. Read existing code that relates to the problem space
3. Launch code-explorer agents to trace execution flows
4. Run read-only experiments (test commands, API calls, benchmarks)
5. Document what works and what doesn't

### Architecture (System Design Analysis)
1. Map the current architecture using code-explorer agents
2. Identify integration points, dependencies, and constraints
3. Research alternative approaches (web search for patterns, best practices)
4. Evaluate trade-offs: performance, maintainability, complexity, team familiarity
5. Document decision criteria and scoring

### Evaluation (Technology/Tool Comparison)
1. Define comparison criteria (performance, maturity, community, licensing, cost)
2. Research each candidate via web search and documentation
3. Check existing codebase for integration constraints
4. Build a comparison matrix with weighted scores
5. Document pros/cons for each option

### Exploration (Codebase Understanding)
1. Start with entry points and trace the flow
2. Launch 2-3 code-explorer agents in parallel for different aspects
3. Map module dependencies and data flow
4. Identify patterns, conventions, and potential issues
5. Document the architecture and key decisions found

## Session Recovery — CRITICAL

**Call `protocol_save_note()` after EVERY significant finding or decision.**

Your analysis, findings, and proposals are NOT auto-saved. Only user messages survive context compaction. Notes are your lifeline for session recovery:

```
protocol_save_note("Finding: Redis caching adds ~50ms latency but reduces DB load by 80%. Trade-off acceptable for our use case.")
protocol_save_note("Decision: Option B (event-driven) preferred over Option A (polling) due to lower resource usage.")
protocol_save_note("Key insight: The auth middleware already supports the plugin pattern we need — see auth/middleware.py:45-80")
```

Save notes for:
- Key findings with evidence
- Decisions made and reasoning
- Important code locations discovered
- Trade-off evaluations
- Questions that arose during exploration

## Agent Usage

- **code-explorer**: Launch for deep codebase analysis. Use 2-3 instances in parallel for different aspects.
- **context-gathering**: Use if you need to build a comprehensive context manifest for a specific area.

## Boundaries

This step is in **discussion mode** (read-only). You cannot:
- Edit source code files
- Create new source code files
- Run destructive commands

If exploration reveals the need for production code changes:
1. **Complete research first**: Finish the research, document findings, then start a `task` protocol for the implementation
2. **Or abort**: If the research question has fundamentally changed, use `protocol_abort` and start fresh

Do NOT try to implement code during research — the protocol enforces this boundary.

## When Exploration is Complete

You should have:
- Enough evidence to answer the research question
- Notes saved for all significant findings (via `protocol_save_note()`)
- A clear picture of what to write in the synthesis step

Advance when ready — no user confirmation required for this step (the user will review during conclusion).
