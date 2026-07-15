---
title: Specialized Agents
tags: [agents, ai-providers, protocols]
created: 2026-05-31
updated: 2026-07-04
sources: [plugin/agents/CLAUDE.md, plugin/agents/*.md, plugin/protocol-configs/sub-protocols/]
---

# Specialized Agents

team-management ships 15 subagent definitions in `plugin/agents/`. Each is a markdown file with YAML frontmatter (`name`, `description`, `tools`, optionally `model`) followed by a system prompt. They exist to do heavy, context-polluting work — codebase exploration, code review, log consolidation, issue sync, external-AI delegation — in a **separate context window**, returning only the result to the main thread. The orchestrator dispatches them with the `Task` tool (`subagent_type: "<name>"`); Claude Code loads the matching `.claude/agents/<name>.md` automatically for its system prompt and tool allow-list. There is no agent-to-agent communication — coordination is file-based through `.claude/state/`.

This page is the catalog. For the codex-cli / agy-cli external-provider mechanics see [AI Provider Integration](pages/subsystems/ai-provider-integration.md); for how subagent dispatch suppresses DAIC enforcement see [DAIC Enforcement](pages/topics/daic-enforcement.md) and the depth-counter detail in [Hooks System](pages/subsystems/hooks-system.md).

## Catalog

Names and tool lists are the frontmatter values. All entries below are universal (usable standalone) except the issue-sync and external-wrapper agents.

### Investigation / architecture
- **context-gathering** (`context-gathering.md`) — builds a comprehensive Context Manifest into a new task file. Invoked at task creation and on task startup when no manifest exists (the `task` protocol's investigation step). Tools include `Edit`/`MultiEdit` because it edits the task file in place. Skipped if the task already has a "Context Manifest" section.
- **context-refinement** (`context-refinement.md`) — at session end, reads the transcript and updates the manifest *only* if drift/new discoveries occurred (speculative — usually a no-op). Invoked during graceful compaction (before a native `/compact`).
- **code-explorer** (`code-explorer.md`) — traces a feature's execution from entry point to storage, maps layers, returns 5-10 essential files. Read-only (no edit tools), `model: sonnet`. Launched 2-3 in parallel in research exploration (`sub-protocols/research-exploration.md`) and task investigation (`task-investigation.md`).
- **code-architect** (`code-architect.md`) — produces an implementation blueprint (files to create/modify, data flows, build sequence). Read-only, `model: sonnet`. One of the 6 brainstorm specialists (`sub-protocols/brainstorm-analysis.md`).

### Brainstorm specialists (the 6 launched in parallel at `brainstorm` analysis step)
Dispatched as a single parallel-dispatch message in `sub-protocols/brainstorm-analysis.md`, each via its own **dedicated `subagent_type`** (`code-architect`, `code-explorer`, `critic`, `user-perspective`, `risk-security-analyst`, `scope-strategist`). The sub-protocol explicitly does NOT paste `.claude/agents/*.md` contents into the `prompt` (those files aren't present on a plugin install), so the specialist frontmatter `tools:` / `model:` are live. The other four:
- **critic** (`critic.md`) — devil's advocate; Necessity + Complexity sections carry explicit YAGNI checks (speculative-scope flag; three-concrete-call-sites rule before introducing an abstraction). `model: sonnet`.
- **user-perspective** (`user-perspective.md`) — end-user/UX impact, capability delta. `model: sonnet`.
- **risk-security-analyst** (`risk-security-analyst.md`) — combined dependency/failure-mode/attack-vector assessment with risk matrices. `model: sonnet`.
- **scope-strategist** (`scope-strategist.md`) — full (not MVP) scope, natural phases, execution order, parallelism. `model: sonnet`.

(code-architect and code-explorer above complete the set of 6.)

### Review
- **code-review** (`code-review.md`) — correctness/security/consistency review. Output template includes a `## ✨ Strengths (1-3)` section before Critical Issues (omitted entirely if nothing genuinely stands out — no performative praise). Findings are rated on the shared **Critical / High / Medium / Low** scale (🔴🟠🟡🟢) also used by code-cleanliness, critic, and risk-security-analyst; the work-log block keeps the gate-parsed `## 🔴 Critical Issues` / `## 🟡 Warnings` section names (Warnings = High + Medium — see the gate-heading gotcha in [task Protocol](pages/protocols/protocol-task.md)). Description says use ONLY when explicitly requested or invoked by a protocol; do NOT use proactively. Launched at the task `code-review` step (`sub-protocols/code-review.md`).
- **spec-compliance-reviewer** (`spec-compliance-reviewer.md`) — **READ-ONLY** agent that diffs the working tree + staging (including untracked files via `git ls-files --others --exclude-standard`) against the task's Success Criteria and returns a binary PASS/FAIL with per-criterion coverage and scope-creep flags. Deliberately omits the `## ✨ Strengths` section. Runs FIRST in the code-review step, before the Claude code-review agent (`sub-protocols/code-review.md`); a PASS lets the orchestrator record the `SPEC_REVIEW: PASSED` sentinel via `protocol_save_note`, which gates the step's `require_spec_review_passed` post_func (see [Protocol Engine](pages/subsystems/protocol-engine.md)).
- **code-cleanliness** (`code-cleanliness.md`) — maintainability analysis (unused code, comment quality, naming, duplication, complexity) with no auto-fix. Not wired into a protocol step; advisory, surfaced via the `/team-management:clean-check` slash command and mentioned as optional in `sub-protocols/code-review.md`. `model: sonnet`.

### Documentation / logging
- **logging** (`logging.md`) — consolidates work logs chronologically into the task's Work Log. Description restricts it to graceful compaction or task completion (the `task` protocol's documentation/completion steps). Tools: `Read, Edit, MultiEdit, LS, Glob`.
- **service-documentation** (`service-documentation.md`) — maintains CLAUDE.md files / module docs as a lean current-state reference (edit-in-place, prune superseded content, no task-name tags / changelog accretion), adapting to super-repo / mono-repo / single-repo layouts. Restricted to graceful compaction / completion or confirmed doc drift.

### Issue-tracking sync
- **Retired (`m-retire-dead-agents-and-daic-cleanup`).** The dedicated `gitlab-sync` / `jira-sync` agents were removed. Task completion is driven by the protocol engine's `completion_dispatch` func + the `sub-protocols/task-completion.md` runtime sub-protocol, not by named agents; issue-tracking operations are exposed directly as `mcp__plugin_team-management_tm__issue_*` MCP tools. See [Issue Tracking Providers](pages/subsystems/issue-tracking-providers.md).

### External AI provider wrappers
- **codex-cli** (`codex-cli.md`) and **agy-cli** (`agy-cli.md`) — thin pass-through wrappers (tools `Read, Bash, Grep, Glob` — no `Write`) that shell out to the `codex` / `agy` CLI under a read-only sandbox and return raw stdout verbatim. The **caller owns the full prompt** (a protocol pre_func builds it). codex runs `codex review --uncommitted` or `codex exec -s read-only`; agy always passes `--sandbox --print-timeout 300s`. agy's OS sandbox blocks shell writes but not its own `write_file` tool, so agy-cli snapshots `git status --porcelain` before/after and prepends an `agy review WARNING: agy modified files…` line on any diff (detect & report, never auto-revert). On failure they return a single `<provider> review unavailable: …` line — never blocking. Full mechanics (registry-driven dispatch, sandbox-flag check via `SandboxFlagError`, credential filter, watchdog/timeout) live in [AI Provider Integration](pages/subsystems/ai-provider-integration.md).

## Mechanics

### Dispatch and context isolation
- The main thread (or a sub-protocol Section 0) issues `Task(subagent_type="<name>", prompt=...)`. Claude Code resolves the system prompt and `tools` allow-list from `.claude/agents/<name>.md`. The agent runs in its own context window; only its returned text re-enters the main conversation.
- Parallel dispatch is a hard requirement in several steps: the code-review step composes **one** message with N+1 `Task` calls (Claude `code-review` + one per enabled provider) — explicitly NOT sequential (`sub-protocols/code-review.md`). Brainstorm analysis composes the 6 specialists + M providers in a single message (`brainstorm-analysis.md`).

### Subagent depth gating (why agents can edit in discussion mode)
Agents like context-gathering, logging, and service-documentation must edit files (task files, CLAUDE.md) even when the main thread is in discussion DAIC mode. This works because `task-transcript-link.py` increments `.claude/state/subagent-depth.json` on `Task`/`Agent` PreToolUse and `post-tool-use.py` decrements it on PostToolUse; while `shared_state.in_subagent_context()` (depth > 0) is true, DAIC enforcement and reminders are suppressed for the agent's tool calls. Both hooks must recognise BOTH `Task` and `Agent` tool names — see [Hooks System](pages/subsystems/hooks-system.md) and [State Files](pages/entities/state-files.md).

### Copy-to-customize rule (do not edit shipped agents in place)
Every shipped agent source carries an HTML-comment banner immediately under its frontmatter:
```
<!-- DO NOT EDIT - managed by team-management; replaced on every update. To customize, copy this file to a new name in .claude/agents/ (e.g. my-code-review.md) and edit the copy. See CLAUDE.tm.md "Customizing shipped agents". -->
```
The plugin ships every `plugin/agents/*.md`; Claude Code loads them from the plugin and they are **replaced on every plugin update**. So **any edit you make to a shipped agent is lost on the next update**. To customize, copy the file to a name the plugin does not ship (e.g. `my-code-review.md`) and edit the copy — the plugin only ever writes the names it ships.

## Gotchas

- **`CLAUDE.md` in the agents dir is NOT an agent.** It is module documentation, not an agent definition (the banner-drift test excludes it; see below).
- **Banner is byte-exact and tested.** `test/test_agent_banner_drift.py` asserts the canonical banner appears byte-for-byte immediately under the frontmatter of every shipped agent (CLAUDE.md excluded). Baking the banner into source keeps `copy_if_changed` a raw-vs-raw compare so reinstalls don't churn unedited agents. A new agent added without the exact banner — truncated, reworded, duplicated, or moved — fails CI.
- **spec-compliance-reviewer's "read-only" is a contract, not a hard wall.** Its frontmatter omits `Write`/`Edit`/`MultiEdit` (the runtime blocks those), but `Bash` is in the list for `git`/`grep`/`wc` inspection and is powerful enough to mutate state if misused. The prompt states read-only is the semantic contract; do not write through Bash.
- **In-step staleness gap on the spec sentinel.** The `SPEC_REVIEW: PASSED` sentinel is structurally invalidated by a backward `protocol_goto`, but NOT by edits made in place within the code-review step (which runs in implementation mode). If you fix anything after the agent returned PASS, re-dispatch spec-compliance-reviewer and re-save the sentinel — this is a discipline rule, not yet enforced structurally (`sub-protocols/code-review.md`).
- **Wrapper trap leaks on hard timeout (codex-cli only).** codex-cli uses `trap '... rm -f' EXIT` for its schema-tmpfile cleanup, but `trap EXIT` does NOT fire on the SIGKILL from `--kill-after`, so a hard-timeout path can leak tmpfiles in `/tmp`. Documented as bookkeeping (non-sensitive content), not a security issue. agy-cli creates a tmpfile only on the shell-native fallback branch (the captured agy output via `mktemp`, removed by a `trap … EXIT`); its git snapshots live in shell variables. Its watchdog kills with SIGTERM, after which the script exits normally and the EXIT trap fires — so agy-cli does not have codex-cli's SIGKILL-leak path.
- **Restricted-use descriptions are load-bearing.** code-review, logging, and service-documentation descriptions say "use ONLY when …" — they are not general-purpose helpers and should not be invoked outside their protocol steps, to avoid redundant churn and spurious doc updates.
