# Wiki Index

Pages are listed below by category. New here? Start with the [Architecture Overview](pages/overview/architecture-overview.md).

## Overview

- [Architecture Overview](pages/overview/architecture-overview.md) — Navigation hub mapping the framework's three runtime planes (hooks, MCP server, `.claude/state` files) and the end-to-end flow of one user message through enforcement and the protocol engine.

## Subsystems

- [Hooks System](pages/subsystems/hooks-system.md) — The Claude Code hook plumbing: event bindings, exit-code contract, per-hook mechanics (enforce/session-start/post-tool-use/user-messages/pre-compact/transcript-link), and cross-hook shared state.
- [Protocol Engine](pages/subsystems/protocol-engine.md) — JSON-driven workflow state machine: step model, per-step DAIC mode, pre/post-func dispatch, audit log, custom-protocol fork/drift, and the `_PHASE_REGISTRY` AI-provider source of truth.
- [MCP Server](pages/subsystems/mcp-server.md) — FastMCP server exposing 42 team-management tools across 8 modules, with single-source-of-truth imports resolved plugin-root-first (`${CLAUDE_PLUGIN_ROOT}/hooks`, `.claude/hooks` legacy fallback) and env-based project-root detection.
- [Plugin Conversion Architecture](pages/subsystems/plugin-conversion.md) — The plugin-era runtime architecture: three-root path model (`PROJECT_DIR`/`PLUGIN_ROOT`/`PLUGIN_DATA`), runtime wiring (hooks shim, MCP cold-start bootstrap, boot-detector, guidance via `@`-includes), the cross-platform `python3` launcher, slash-command vs MCP-tool namespacing (canonical `mcp__plugin_team-management_tm__*` + drift guard), and provider-token continuity.
- [AI Provider Integration](pages/subsystems/ai-provider-integration.md) — Registry-driven 6-phase dispatch of Codex/agy as parallel Task agents: `_PHASE_REGISTRY`, template lookup, credential filter (17 patterns + PEM pass), sandbox-flag check (`SandboxFlagError`), agy watchdog, and the config-flow setup.
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
- [retro Protocol](pages/protocols/protocol-retro.md) — 4-step branchless team retrospective from git + protocol logs: the stale-anchor guard with window-recoverable blocks, the typed cross-task log reader (injection boundary), deterministic v1 metrics, the forgery-resistant NUL-separated harvest, and the credential-filtered report + typed trend snapshot in docs/retros/.

## Topics

- [DAIC Enforcement](pages/topics/daic-enforcement.md) — Internals of DAIC mode gating in `sessions-enforce.py`: three modes, blocked tools, subagent/admin/wiki bypasses, the corrected execution order, branch enforcement, and optimize frozen-path blocking.
- [Context Preservation](pages/topics/context-preservation.md) — How the framework survives context compaction and session restarts: auto-compact trips, the PreCompact checkpoint, and post-compact/session restoration.
- [Review Loop Convergence](pages/topics/review-loop-convergence.md) — Why the code-review step ends: three-level severity, claim-not-verdict confirmation, routing by fix depth, history in re-reviews, spec audit last, and tree-hash freshness gates.
- [CLAUDE.md Hygiene](pages/topics/claude-md-hygiene.md) — The content test for CLAUDE.md lines and the blocking documentation-step gate on task names, history wording, review chatter and the line budget.

## Entities

- [Specialized Agents](pages/entities/specialized-agents.md) — Catalog of the 17 shipped subagents (investigation, review, logging, external-AI wrappers), their dispatch via `Task`/`subagent_type`, depth-gated DAIC bypass, the copy-to-customize rule, and the public five-role-group team framing.
- [State Files](pages/entities/state-files.md) — Catalog of `.claude/state/` files (task, DAIC mode, subagent depth, optimize, mappings, flags) with the `shared_state` durable-write/lock helpers and the fsync state-sync-race fix.
- [Configuration Schema](pages/entities/configuration-schema.md) — Reference catalog of every `team-management/config.json` key (providers, AI phases, branch prefixes, `test_command` allowlist, auto_compact) and how each is loaded and consumed.
- [Review Output Contract](pages/entities/review-output-contract.md) — The unified review-agent output contract: confidence calibration + display rules with the Critical exception, quote-gate with secrets redaction, AUTO-FIX/ASK advisory metadata, FP suppressions, the JSONL findings schema (fingerprint, reserved verdict), and the gate-parsed sentinel-heading invariants.
- [Completion Honesty Contract](pages/entities/completion-honesty-contract.md) — The spec-audit honesty contract: per-item Verification Modes and six statuses with the strict verdict, the SPEC_UNVERIFIABLE carriage chain (staleness check, legacy carry-list), per-item UNVERIFIABLE confirmation at completion with widget-accurate batching, and the fail-open prohibitions on both the review and completion sides.
- [Behavior Rules Contract](pages/entities/behavior-rules-contract.md) — The gstack behavioral-rules adoption across five guidance carriers: User Sovereignty + User Challenge (400-word budget), rationalization rows, byte-identical question-grounding block, selective forcing questions, Session Hygiene + the shipped-protocols-only decision tree, and the section-bounded drift guard.
- [Review Dispatch Gate](pages/entities/review-dispatch-gate.md) — The CR-2 classifier gate for the review chain: `classify_review_dispatch` diff measurement (`-z` verbatim paths, thresholds ≥50 / >200, docs/config-only skip, framework-path exclusion with a `framework_lines_excluded` tally), the three `code_review.*_enabled` fail-enabled gates, the §2/§2a orchestrator contract (first round + second-echelon red-team, add-only re-measurement), and the project-scoped JSONL dispatch telemetry.
- [Plugin Update Notice](pages/entities/update-notice.md) — The once-per-update session-start announcement: hybrid delivery (a deterministic `systemMessage` banner for the user + missed changelog sections as model context), shipped CHANGELOG.md, the persistent `tm-last-seen-version.json` marker (PLUGIN_DATA primary, `.claude/state` fallback), fail-open wiring, the one-release transition window that lets the release introducing the mechanism announce itself to markerless installs, the CI changelog gate, and changelog-sourced release notes.
- [Safety Guards](pages/entities/safety-guards.md) — The destructive-Bash-command guard in the PreToolUse hook: segment-aware classifier (rm -rf, SQL DROP/TRUNCATE, git force/reset/checkout-restore-dot/clean, kubectl delete, docker rm -f/prune), exit-2 block + nonce-file typed confirmation with exact raw-command binding, sensitivity levels, the reflog-recoverable own-branch exception, the fail-closed boundary, and the documented friction-only bypass list.

## Procedures

- [Completion and Git Flow](pages/procedures/completion-and-git-flow.md) — How the completion step dispatches git/issue work: the provider-driven chain vs the disabled-provider 4-option menu (merge_local/push_pr/keep/discard), branch-safety, default-branch detection, gh-pr idempotency, the typed discard gate, and optimize squash.
