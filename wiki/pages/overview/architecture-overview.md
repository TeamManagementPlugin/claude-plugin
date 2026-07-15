---
title: Architecture Overview
tags: [architecture, hooks, protocols, mcp]
created: 2026-05-31
updated: 2026-07-11
sources: [plugin/__init__.py, plugin/CLAUDE.md, plugin/hooks/sessions-enforce.py, plugin/hooks/protocol_engine.py, plugin/hooks/shared_state.py, plugin/hooks/post-tool-use.py, plugin/hooks/session-start.py, plugin/mcp/server.py, plugin/protocol-configs/task.json]
---

# Architecture Overview

team-management is a Claude Code add-on that turns the assistant into a workflow-enforced pair-programmer. It has no long-running service of its own: it is a collection of **Python hooks** (run by Claude Code on tool/message/session events), a **stateless MCP server** (exposes 42 tools), a **JSON-driven protocol engine** (drives multi-step workflows), and a set of **`.claude/state/` files** that the two halves read and write. The hooks enforce; the MCP server mutates; the state files are the only shared channel between them. This page maps the pieces and the end-to-end request flow; every subsystem has its own page (linked below) for the internals.

## The three runtime planes

There is no shared process memory. The hooks and the MCP server are separate OS processes spawned by Claude Code, and they coordinate **only through JSON files under `.claude/state/`**. This is the single most important fact about the architecture:

1. **Hook plane** (`plugin/hooks/*.py`) — short-lived processes Claude Code execs on `PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `SessionStart`, `PreCompact`. They *read* state to enforce and inject; they generally do not author the protocol/task state (the exception is depth/throttle bookkeeping). `sessions-enforce.py` is `PreToolUse` and can block a tool by `sys.exit(2)`.
2. **MCP plane** (`plugin/mcp/`) — a FastMCP server (`server.py` `mcp = FastMCP("team-management")`) that owns the **writers** of protocol/DAIC/task state via the `ProtocolEngine` and the DAIC tools. The hook plane is read-mostly; the MCP plane is write-mostly.
3. **State plane** (`.claude/state/`) — the contract surface. See [State Files](pages/entities/state-files.md) for the full inventory. Writers go through `shared_state.py`, which does atomic temp-file + `os.fsync` + `os.replace` writes (`shared_state.py` `_write_json_durable`) so a hook never reads a half-written file.

Both planes import the *same* provider/state code from `.claude/hooks/` — the MCP server does a `sys.path` bootstrap (`server.py`, `core/project.setup_provider_imports()`) rather than keeping its own copy. This "single source of truth" import strategy is deliberate: an earlier bug had installers copying `gitlab_utils.py` into the MCP tree, causing version skew.

## End-to-end flow of one user message

Walking a single turn from message to tool execution:

1. **`UserPromptSubmit` → `user-messages.py`.** Resets the subagent depth counter to 0 (self-heals leaks from interrupted Tasks), surfaces read-only task hints, and warns if on `master`/`main` with no active task. (Trigger-phrase mode switching is legacy; the protocol-first model now drives DAIC mode through the engine.)

2. **Claude proposes a tool call → `PreToolUse` → `sessions-enforce.py`.** This is the enforcement gate, and **order matters** (it was a bug-fix to get it right). The hook loads stdin (`sessions-enforce.py`), then runs checks in this sequence:
   - **Workflow bypass** — `check_workflow_bypass()`; if set, `sys.exit(0)` and skip everything. This is the user-only escape hatch.
   - **Protected-path enforcement** (`PROTECTED_PATHS`) — blocks Read/Grep/Edit/Write/Bash on `.claude/state/current_task.json`, `daic-mode.json`, `protocol-logs/`, `optimize-state.json`, and `team-management/protocol-configs/system/`. Runs **before** the subagent bypass so even subagents cannot touch state directly.
   - **Frozen-path enforcement** (optimize protocol) — `is_frozen_path()` for writes and `_bash_targets_frozen()` for Bash; only active during the `experimentation` step. See [Optimize Protocols](pages/protocols/optimize-protocols.md).
   - **Config protection** — blocks writing the `protocol_engine` section of `config.json`.
   - **Subagent bypass → administrative whitelist → DAIC enforcement → branch enforcement** — the DAIC mode check (discussion blocks edit tools; documentation blocks source but allows docs) and the task→branch mapping check. See [DAIC Enforcement](pages/topics/daic-enforcement.md) and the `DEFAULT_CONFIG.branch_prefixes` table at `sessions-enforce.py`.

3. **Tool runs** (if not blocked). If it is an MCP protocol/DAIC tool, the **MCP plane** mutates state (see below). If it is an edit/Bash, it just runs.

4. **`PostToolUse` → `post-tool-use.py`.** Decrements the subagent depth counter for finishing Task/Agent dispatches, runs an **auto-compact** token check at the configured threshold (default 85%), injects the **active protocol step's end condition** on a throttle (first call after any state change, then every 10th —), appends an auto-worklog line every 8th significant tool during implementation, and fires provider auto-sync if a task status changed.

5. **`SessionStart` → `session-start.py`.** On a fresh/compacted session it clears the auto-compact flags, resets subagent depth, then **restores DAIC mode from the active protocol step** and injects the step's start+end text. This is how a workflow survives a session restart: the protocol state in `current_task.json` is the resume point, not conversation history.

## The protocol engine: the workflow brain

The engine (`protocol_engine.py`, `class ProtocolEngine(OptimizeCompletionMixin, AIProvidersMixin)`) runs **inside the MCP server process** and is the only thing that legitimately writes protocol/DAIC/task state. A protocol is a JSON file (`team-management/protocol-configs/`) listing ordered steps, each with a `mode` (the DAIC mode to force on entry), `pre_funcs`, `post_funcs`, `advance_args`, and `start`/`end` text — see `task.json` for the canonical 5-step shape (investigation → implementation → code-review → documentation → completion).

The lifecycle MCP tools map to engine methods:
- `protocol_start` → `start_protocol` — loads config, sets step 0's DAIC mode, runs step 0's `pre_funcs`. Auto-resumes a looping protocol if one is already active.
- `protocol_advance` → `advance_step` — runs the *current* step's `post_funcs` (injecting `advance_summary` into their args), and if `post_funcs_stop_on_failure` and a func returns `chain_stopped`, the protocol does **not** advance. Otherwise it logs, updates frontmatter, and either loops the same step (`looping_step`) or moves to the next and applies its mode.
- `protocol_goto` → `goto_step`, `protocol_abort` → `abort_protocol`.

`pre_funcs`/`post_funcs` are named handlers resolved by `_build_handlers` and enumerated by `get_available_funcs`. They are the engine's automation: `git_setup_branch`, `create_task_file`, `create_issue_if_enabled`, `verify_tests_pass`, `require_spec_review_passed`, `completion_dispatch`, the AI-provider resolvers, and the optimize funcs. See [Protocol Engine](pages/subsystems/protocol-engine.md) for the func catalogue and [Workflow Protocols](pages/protocols/workflow-protocols.md) for the per-protocol step maps.

Each state-mutating step funnels through `shared_state.set_protocol_state` / `clear_protocol_state`, which also **delete `protocol-end-condition-counter.txt`** as a side effect — that deletion is the *sole* reset mechanism for the post-tool-use end-condition injection throttle. There is no step tracking in the hook; engine-driven file deletion is the entire coordination protocol.

## Where each kind of state lives

- **Active workflow** — `current_task.json` holds the `protocol` block (name, step index, step name, loop iteration) plus task identity (task, branch, services). PROTECTED; MCP-only.
- **DAIC mode** — `daic-mode.json`. PROTECTED; written by the engine per step or the `daic_mode_switch_*` tools.
- **Audit trail** — `.claude/state/protocol-logs/<task>.json`, one entry per step transition/goto/abort/note. PROTECTED.
- **Per-task** — `.claude/state/tasks/<task>/` (subagent transcript staging, cleaned up on completion). The former multi-session `session.json` / context-warning-flag scaffold was removed in l-dead-code-removal.
- **Subagent depth** — `subagent-depth.json`, a file-locked integer counter (`in_subagent_context()` == depth > 0) that gates DAIC bypass and reminder/auto-sync suppression for nested agents.
- **Provider mappings** — `gitlab-mappings.json` / `jira-mappings.json` / `github-mappings.json`.
- **Optimize** — `optimize-state.json` (frozen paths + baselines). PROTECTED.
- **Config** — `team-management/config.json` (project), `.claude/settings.json` (hook + MCP registration). Both planes read config; the `protocol_engine` section is write-protected from the agent.

Full schema: [State Files](pages/entities/state-files.md) and [Configuration Schema](pages/entities/configuration-schema.md).

## Peripheral subsystems (linked, not detailed here)
- [Issue Tracking Providers](pages/subsystems/issue-tracking-providers.md) — GitLab/Jira/GitHub-Gitea wrappers in `hooks/`, imported by both planes (single source of truth).
- [AI Provider Integration](pages/subsystems/ai-provider-integration.md) — Codex/agy run as parallel Task agents, dispatched by the registry-driven `pre_funcs` in 6 phases.
- [Specialized Agents](pages/entities/specialized-agents.md) — context-gathering, code-review, spec-compliance-reviewer, etc., each in its own context window.
- [Context Preservation](pages/topics/context-preservation.md) — auto-compact, PreCompact checkpointing, session-start restoration.
- [Completion and Git Flow](pages/procedures/completion-and-git-flow.md) — the `completion_dispatch` func and the provider-driven vs disabled-provider 4-option menu.
- [LLM Wiki Feature](pages/subsystems/llm-wiki-feature.md) — the knowledge base this page lives in.

## Design decisions and rationale

- **File-only coordination between planes.** Hooks and the MCP server are separate processes; the only durable channel is `.claude/state/`. This is why every writer uses atomic `_write_json_durable` (`shared_state.py`) — a hook reading mid-write would corrupt enforcement decisions.
- **Enforcement order is load-bearing.** In `sessions-enforce.py`, protected-path and subagent/whitelist checks run *before* DAIC enforcement. The reverse order (an earlier bug) blocked specialized agents from editing task files and blocked task-file creation in discussion mode.
- **MCP server is stateless and import-only.** It copies no provider code; it `sys.path`-bootstraps `.claude/hooks/` (`server.py`). Tools are registered per-module via `register_tools(mcp)` (`server.py`), keeping the entry point at ~90 lines.
- **`workflow_toggle` was intentionally removed from MCP** (`server.py`). Letting Claude disable its own enforcement is a backdoor; bypass is user-only via `workflow_command.py`.
- **Engine composes mixins.** The ~3,900-line engine was split along the `_build_handlers` seam into `ai_providers.py` (`AIProvidersMixin`) and `optimize_completion.py` (`OptimizeCompletionMixin`), with shared subprocess timeouts hoisted to `engine_constants.py` to break an import cycle. Mixins keep *qualified* `subprocess.run(...)` so `patch("protocol_engine.subprocess.run")` still intercepts them.

## Gotchas

- **Don't trust `__init__.py` for the version.** `plugin/__init__.py` is a package marker; any `__version__` string there can lag the real packaging version in `pyproject.toml`. Treat code (and `pyproject.toml`) as ground truth.
- **There are two `CLAUDE.md` trees.** The repo root `CLAUDE.md` and `plugin/CLAUDE.md` are both living docs and can drift from each other. Treat code as ground truth.
- **The legacy DAIC `daic`-substring Bash false-trigger was removed** (`m-retire-dead-agents-and-daic-cleanup`). A discussion-mode Bash command containing the token `daic` (even inside a grep pattern) is no longer intercepted; the old shim was deleted from `sessions-enforce.py` (regression test `test/test_sessions_enforce_daic.py`).
- **The end-condition injection throttle has no step memory.** The hook only checks a counter file; the *only* thing that resets it is the engine deleting `protocol-end-condition-counter.txt` inside `set_protocol_state`/`clear_protocol_state`. If you add a new protocol-state writer, it must funnel through those two functions or the next tool call will not re-inject the step's end condition.
- **Protected paths block agent *reads* too.** `customize_protocol` must run in the MCP server process precisely because reading the `system/` tree is hook-blocked for agent tools (`sessions-enforce.py`).
- **Subagent depth is a counter, not a boolean, for a reason.** Parallel same-type subagents share dispatch; a boolean cleared on the first finish leaked injection into still-running siblings. Both the increment (`task-transcript-link.py`) and decrement (`post-tool-use.py`) must recognize *both* `Task` and `Agent` tool names — some harnesses name the dispatch tool `Agent`.
