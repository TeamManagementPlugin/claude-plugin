# Wiki Index

Pages are listed below by category. New here? Start with the [Architecture Overview](pages/overview/architecture-overview.md).

## Overview

- [Architecture Overview](pages/overview/architecture-overview.md) — Navigation hub mapping the framework's three runtime planes (hooks, MCP server, `.claude/state` files) and the end-to-end flow of one user message through enforcement and the protocol engine.

## Subsystems

- [Hooks System](pages/subsystems/hooks-system.md) — The Claude Code hook plumbing: event bindings, exit-code contract, per-hook mechanics (enforce/session-start/post-tool-use/user-messages/pre-compact/transcript-link), and cross-hook shared state.
- [Protocol Engine](pages/subsystems/protocol-engine.md) — JSON-driven workflow state machine: step model, per-step DAIC mode, pre/post-func dispatch, audit log, custom-protocol fork/drift, and the `_PHASE_REGISTRY` AI-provider source of truth.
- [MCP Server](pages/subsystems/mcp-server.md) — FastMCP server exposing 41 team-management tools across 8 modules, with single-source-of-truth imports resolved plugin-root-first (`${CLAUDE_PLUGIN_ROOT}/hooks`, `.claude/hooks` legacy fallback) and env-based project-root detection.
- [Plugin Conversion Architecture](pages/subsystems/plugin-conversion.md) — The plugin-era runtime architecture: three-root path model (`PROJECT_DIR`/`PLUGIN_ROOT`/`PLUGIN_DATA`), runtime wiring (hooks shim, MCP cold-start bootstrap, boot-detector, guidance via `@`-includes), the cross-platform `python3` launcher, slash-command vs MCP-tool namespacing (canonical `mcp__plugin_team-management_tm__*` + drift guard), and provider-token continuity.
- [AI Provider Integration](pages/subsystems/ai-provider-integration.md) — Registry-driven 6-phase dispatch of Codex/agy as parallel Task agents: `_PHASE_REGISTRY`, template lookup, credential filter (16 patterns + PEM pass), sandbox-flag check (`SandboxFlagError`), agy watchdog, and the config-flow setup.
- [Issue Tracking Providers](pages/subsystems/issue-tracking-providers.md) — Multi-provider issue-tracking layer (GitLab/GitHub/Gitea/Jira): provider ABCs, per-provider quirks, task⇄issue sync, mapping files, and structured error reporting.
- [LLM Wiki Feature](pages/subsystems/llm-wiki-feature.md) — The opt-in LLM Wiki subsystem: ingest/tune/lint slash commands, config-flow seeding, page format, the unconditional `wiki/` DAIC whitelist, and the documentation-step `wiki_update_reminder` pre_func.

## Protocols

- [Workflow Protocols](pages/protocols/workflow-protocols.md) — Hub: the shared JSON step schema, `@sub-protocols` resolution, the two-stage code-review gate, family-level design decisions and gotchas, and a catalog linking to all 6 protocol pages.
- [task Protocol](pages/protocols/protocol-task.md) — Standard 5-step implementation lifecycle (investigation → implementation → code-review → documentation → completion) with the two-stage spec-compliance + code-quality review gate.
- [brainstorm Protocol](pages/protocols/protocol-brainstorm.md) — 5-step parallel-specialist ideation (6 specialists + conflict-resolution loop) producing a results document and a coverage-audited set of implementation tasks; no production code.
- [research Protocol](pages/protocols/protocol-research.md) — 4-step spike / PoC / evaluation investigation; produces a knowledge artifact, with no git branch and no code.
- [refactoring Protocol](pages/protocols/protocol-refactoring.md) — 6-step test-baseline-gated restructuring with per-increment testing and a regression-verify gate against the baseline.
- [optimize Protocol](pages/protocols/protocol-optimize.md) — 7-step metric-driven optimization in interactive batched mode (discussion checkpoints between batches).
- [optimize-unattended Protocol](pages/protocols/protocol-optimize-unattended.md) — Autonomous twin of optimize; experimentation runs to a termination condition with no batch checkpoints (overnight runs).
- [Optimize Protocols](pages/protocols/optimize-protocols.md) — Shared engine mechanics for the optimize pair: the looping-step primitive, engine-owned measurement (TSV leaderboard), frozen paths, the resume credential scan, and squash+leaderboard completion.

## Topics

- [DAIC Enforcement](pages/topics/daic-enforcement.md) — Internals of DAIC mode gating in `sessions-enforce.py`: three modes, blocked tools, subagent/admin/wiki bypasses, the corrected execution order, branch enforcement, and optimize frozen-path blocking.
- [Context Preservation](pages/topics/context-preservation.md) — How the framework survives context compaction and session restarts: auto-compact trips, the PreCompact checkpoint, and post-compact/session restoration.

## Entities

- [Specialized Agents](pages/entities/specialized-agents.md) — Catalog of the 17 shipped subagents (investigation, review, logging, issue-sync, external-AI wrappers), their dispatch via `Task`/`subagent_type`, depth-gated DAIC bypass, and the copy-to-customize rule.
- [State Files](pages/entities/state-files.md) — Catalog of `.claude/state/` files (task, DAIC mode, subagent depth, optimize, mappings, flags) with the `shared_state` durable-write/lock helpers and the fsync state-sync-race fix.
- [Configuration Schema](pages/entities/configuration-schema.md) — Reference catalog of every `team-management/config.json` key (providers, AI phases, branch prefixes, `test_command` allowlist, auto_compact) and how each is loaded and consumed.

## Procedures

- [Completion and Git Flow](pages/procedures/completion-and-git-flow.md) — How the completion step dispatches git/issue work: the provider-driven chain vs the disabled-provider 4-option menu (merge_local/push_pr/keep/discard), branch-safety, default-branch detection, gh-pr idempotency, the typed discard gate, and optimize squash.
