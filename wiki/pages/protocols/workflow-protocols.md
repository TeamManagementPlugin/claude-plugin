---
title: Workflow Protocols
tags: [protocols, agents, daic, ai-providers]
created: 2026-05-31
updated: 2026-07-28
sources: [plugin/protocol-configs/task.json, plugin/protocol-configs/brainstorm.json, plugin/protocol-configs/research.json, plugin/protocol-configs/refactoring.json, plugin/protocol-configs/optimize.json, plugin/protocol-configs/optimize-unattended.json, plugin/protocol-configs/sub-protocols/code-review.md, plugin/hooks/shared_state.py]
---

# Workflow Protocols

This is the **hub page** for the 6 shipped workflow protocols. It documents what they have in common — the JSON step schema, the `@sub-protocols/*.md` resolution mechanism, the two-stage code-review gate, and the family-level design decisions — and links out to a dedicated detail page for each protocol. For the engine that reads these configs and drives the state machine, see [Protocol Engine](pages/subsystems/protocol-engine.md).

A workflow protocol is a JSON config shipped in the plugin at `plugin/protocol-configs/` (read at runtime from `${CLAUDE_PLUGIN_ROOT}/protocol-configs/`; legacy installs also kept a copy at `team-management/protocol-configs/system/`) that drives a multi-step lifecycle: each step declares a DAIC mode, a block of `pre_funcs`/`post_funcs` the engine auto-executes, and a `start` text shown to the LLM on entry. The protocols are the only sanctioned way to do implementation work — they manage [DAIC mode](pages/topics/daic-enforcement.md) transitions, task files, git branches, and completion automatically so the LLM never has to flip modes by hand.

## The 6 Shipped Protocols — Catalog

| Protocol | Steps | Purpose | Detail page |
|----------|-------|---------|-------------|
| **task** | 5: investigation → implementation → code-review → documentation → completion | Standard implementation lifecycle | [task Protocol](pages/protocols/protocol-task.md) |
| **brainstorm** | 5: topic → discussion → analysis → results → planning | Parallel-specialist ideation → planned tasks (no code) | [brainstorm Protocol](pages/protocols/protocol-brainstorm.md) |
| **research** | 4: scoping → exploration → synthesis → conclusion | Spikes / PoCs / evaluations (no branch, no code) | [research Protocol](pages/protocols/protocol-research.md) |
| **refactoring** | 6: test-baseline → planning → refactoring → test-verify → code-review → completion | Test-baseline-gated restructuring | [refactoring Protocol](pages/protocols/protocol-refactoring.md) |
| **optimize** | 7: setup → metric-script → baseline → experimentation* → synthesis → code-review → completion | Metric-driven optimization, interactive batched | [optimize Protocol](pages/protocols/protocol-optimize.md) |
| **optimize-unattended** | 7: same as optimize | Autonomous twin of optimize (no batch checkpoints) | [optimize-unattended Protocol](pages/protocols/protocol-optimize-unattended.md) |

*`experimentation` is a `looping_step`. The two optimize protocols share every sub-protocol verbatim except experimentation; their deep, shared engine mechanics are documented in [Optimize Protocols](pages/protocols/optimize-protocols.md).

## JSON Step Schema

Each config is `{name, description, steps: [...]}`. A step object carries (see `task.json` for a representative example):

- `name` / `description` — step identity; `description` surfaces in `protocol_list()`.
- `mode` — the DAIC mode this step runs in: `discussion` (read-only), `implementation` (full edit), or `documentation` (docs/CLAUDE.md/task-file edits only, source blocked). The engine applies this mode on step entry.
- `start` — the step's prompt text. Either inline markdown (e.g. `task.json`, the documentation step) or an `@sub-protocols/<name>.md` reference (e.g. `task.json`).
- `pre_funcs` — engine funcs run on step *entry* (and re-run each loop iteration for looping steps).
- `post_funcs` — engine funcs run on `protocol_advance` *out* of the step. With `post_funcs_stop_on_failure: true`, the chain halts on the first func returning `success=False` and the protocol does **not** advance.
- `advance_args` — argument names the caller must pass to `protocol_advance` (e.g. `task.json` requires `task`, `branch`, `task_content`).
- `end` — the step's completion condition, formatted as a **markdown checkbox list** (1–6 `- [ ]` items with optional prose framing; completion/conclusion steps carry a single user-confirmation checkbox plus informational engine-automation lines; arg-passing instructions come last). Injected verbatim into stderr by `post-tool-use.py` (first tool call after a protocol-state change, then every 10th) and returned verbatim by the protocol MCP tools and session-start context. Format drift-guarded by `test_protocol_end_checklist.py` (line-start checkbox required per non-empty `end`, plus a no-empty-`end` companion guard).
- `looping_step: true` — re-runs the same step instead of advancing (optimize experimentation only; see [Optimize Protocols](pages/protocols/optimize-protocols.md)).
- `skip_notification: true` — suppresses the step-complete notification (e.g. `task.json`).

### How `@sub-protocols/*.md` is pulled in

The `start` value `@sub-protocols/task-investigation.md` is resolved by `shared_state.resolve_protocol_start_text` (`shared_state.py`). It strips the leading `@`, then walks an ordered `search_bases` list — `custom/` → `${CLAUDE_PLUGIN_ROOT}/protocol-configs/` → `${CLAUDE_PLUGIN_ROOT}/` → legacy `team-management/protocol-configs/system/` → `team-management/` → `.claude/` — returning the first file that exists (`shared_state.py`). Custom-first ordering is what makes a forked `custom/` protocol override the system copy with no engine change. **Resolution is single-level**: the resolver expands only the top-level `@sub-protocols/...` ref; `@knowledge/*.md` references *inside* the sub-protocol body are NOT auto-expanded — the LLM must open those with its Read tool (the markdown wraps mandatory ones in "READ … Do not continue without reading it" directives, e.g. `code-review.md`).

## The Two-Stage code-review Gate

`task` and the optimize pair share a structurally-gated `code-review` step that is worth calling out because it is the framework's strongest quality gate (and because `refactoring`'s review is deliberately lighter — see its page):

1. **Stage 1 — spec compliance** (`code-review.md`): the read-only `spec-compliance-reviewer` agent audits the diff against the task's Success Criteria. On PASS the orchestrator writes the sentinel `SPEC_REVIEW: PASSED` verbatim via `protocol_save_note`. `require_spec_review_passed` runs as both a `pre_func` (entry reminder) and a `post_func` (hard block — advance is impossible without the sentinel).
2. **Stage 2 — code quality** (`code-review.md`): one message dispatches the Claude `code-review` agent *plus* one Task per configured AI provider (`codex-cli` / `agy-cli`), all in parallel. Findings are aggregated with equal weight; provider output is advisory and never blocks.

The advance summary itself is gated by `check_completion_evidence` (`code-review.md`): the `summary` arg must contain verification evidence (a fenced output block, `N/N passed`, `exit 0`, etc.) or the literal escape hatch `no-verification-applicable: <reason>` — prose like "looks good" is rejected. See [Specialized Agents](pages/entities/specialized-agents.md) for the reviewer agents and [AI Provider Integration](pages/subsystems/ai-provider-integration.md) for the per-phase provider wiring.

## Design Decisions

- **JSON-as-config, markdown-as-prompt.** Step orchestration (mode, func chains, args) lives in compact JSON; the verbose human-facing instructions live in separate sub-protocol markdown. This keeps the JSON diffable and lets the long prompts be edited/forked without touching engine logic.
- **Funcs over hardcoded steps.** All side effects (branch creation, issue sync, archiving, squashing) are named funcs in `pre_funcs`/`post_funcs`, discoverable via `protocol_available_funcs`. Adding behavior to a step is a JSON edit, not an engine edit.
- **`post_funcs_stop_on_failure` as a structural gate.** Setting it `true` turns a post_func into a hard precondition for advancing — this is how spec-compliance, completion-evidence, and the optional test gate become un-bypassable rather than advisory.
- **AI providers wired per-phase, not globally.** Each phase has its own `resolve_ai_providers_for_<phase>` pre_func gated by an `include_in_<phase>` config key (see [AI Provider Integration](pages/subsystems/ai-provider-integration.md)), so a user can enable codex on code-review only.
- **Shared sub-protocols for the optimize pair.** Rather than duplicate 6 of 7 steps, both optimize protocols `@`-reference the same markdown; only experimentation diverges. A drift-guard test asserts the shared blocks stay byte-identical.

## Family-Level Gotchas

These cut across protocols; each protocol's own page lists its specific traps.

- **Two completion paths, and `research` uses neither dispatcher.** `task`, `brainstorm` (planning), `refactoring`, `optimize`, and `optimize-unattended` route through `completion_dispatch` (provider-driven chain, or the disabled-provider 4-option menu). `research`'s conclusion runs the plain `archive_task → update_issue_status → cleanup → clear` chain with `post_funcs_stop_on_failure: false`. Don't assume the menu applies to research.
- **`refactoring`'s code-review is lighter than `task`/optimize's.** It omits `require_spec_review_passed` and `check_completion_evidence` — no spec-compliance sentinel there (the test baseline provides the behavioural guarantee instead).
- **`@knowledge/*.md` refs inside a sub-protocol are not auto-resolved.** The resolver expands only the single top-level `@sub-protocols/...` ref. Nested knowledge refs are prose breadcrumbs the LLM must Read manually; relying on auto-expansion silently skips required reading.
- **Two different `@-ref` mechanisms exist.** `CLAUDE.tm.md` / `CLAUDE.md` use Claude Code's *native* loader (root-relative, hence `@team-management/knowledge/*.md`). Protocol step text uses `resolve_protocol_start_text`'s `search_bases` walk (supports the shorter `@knowledge/foo.md`). Not interchangeable.
- **Documentation-mode steps block source edits, not just "discourage" them.** In `task`'s documentation step and all of brainstorm/research-synthesis, an Edit to a source file is hook-blocked; the prompt instructs `protocol_goto` back to implementation rather than papering over it.
- **branch is `none` for research; `optimize/` and `brainstorm/` are special prefixes.** Task-type→branch mapping (`task-investigation.md`) maps `o-` → `optimize/` and `b-` → `brainstorm/` as special cases outside the generic `implement-`/`fix-`/`refactor-` logic; research tasks deliberately have no branch.
