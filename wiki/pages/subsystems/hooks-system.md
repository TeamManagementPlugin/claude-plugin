---
title: Hooks System
tags: [hooks, architecture, daic]
created: 2026-05-31
updated: 2026-07-14
sources: [plugin/hooks/sessions-enforce.py, plugin/hooks/session-start.py, plugin/hooks/post-tool-use.py, plugin/hooks/user-messages.py, plugin/hooks/pre-compact.py, plugin/hooks/task-transcript-link.py, plugin/hooks/config_intent_gate.py, plugin/hooks/hooks.json, plugin/hooks/hook_utils.py, plugin/hooks/shared_state.py, test/test_hooks_matcher_drift.py, test/test_sessions_enforce_daic.py, test/test_hook_stdin_guards.py]
---

# Hooks System

The hook layer is how team-management injects behavior into Claude Code. Claude Code fires Python scripts at well-defined lifecycle events (tool calls, prompt submission, session start, compaction); each script reads a JSON event payload on `stdin` and communicates back through exit codes and stdout/stderr. This is the enforcement and context-injection plumbing — the actual DAIC policy decisions, protocol-step logic, and compaction strategy are documented in [DAIC Enforcement](pages/topics/daic-enforcement.md), [Protocol Engine](pages/subsystems/protocol-engine.md), and [Context Preservation](pages/topics/context-preservation.md). This page covers the wiring: which event each script binds to, what it consumes, what it emits, and the cross-hook state it reads/writes.

## Event Bindings and Registration

Hooks are registered through the plugin manifest `plugin/hooks/hooks.json`, which Claude Code merges into the session's hook configuration. The manifest maps Claude Code event names to shim-launched command entries:

| Event | Script | Matcher |
|-------|--------|---------|
| `UserPromptSubmit` | `user-messages.py` | (none — event has no matcher) |
| `UserPromptSubmit` | `config_intent_gate.py` | (none — event has no matcher) |
| `UserPromptExpansion` | `config_intent_gate.py` | (none — event has no matcher) |
| `PreToolUse` | `sessions-enforce.py` | `Write\|Edit\|MultiEdit\|NotebookEdit\|Task\|Bash\|Read\|Grep` |
| `PreToolUse` | `task-transcript-link.py` | `Task\|Agent` |
| `PostToolUse` | `post-tool-use.py` | (none — runs for all tools) |
| `SessionStart` | `session-start.py` | `startup\|clear` |
| `PreCompact` | `pre-compact.py` | `auto\|manual` |

Two distinct `PreToolUse` matchers run for different purposes: the enforcement hook only fires for the tools it polices, while the transcript-link hook fires for subagent dispatch (see the two `PreToolUse` matchers in `plugin/hooks/hooks.json`). Both `Task` and `Agent` appear in the second matcher because some Claude Code harnesses name the subagent-dispatch tool `Agent` — see the Gotchas section.

A second `UserPromptSubmit` hook plus a dedicated `UserPromptExpansion` binding both invoke `config_intent_gate.py` — the deterministic gate that detects a physical `/team-management:config` (or `/team-management:init`) invocation and opens the short-lived config-write window so `config_update` may run (8 hook commands across 6 events in total). `/team-management:init` is included so init can drive `config_update` (e.g. set `wiki.enabled`) through the same validated, intent-gated path. It runs outside the LLM and always exits 0.

### Exit-code contract

Each hook follows the Claude Code convention (`plugin/knowledge/claude-code/hooks-reference.md`):

- **Exit 0** — success; tool proceeds. For `UserPromptSubmit`/`SessionStart`, stdout is injected into context; for other events stdout is only shown in transcript mode.
- **Exit 2** — blocking error; stderr is fed back to Claude. For `PreToolUse` this blocks the tool call; for `PostToolUse` the tool already ran, so stderr is just surfaced to Claude.
- The hooks also use the structured JSON output form (`hookSpecificOutput`) when they need finer control — `session-start.py` and `user-messages.py` emit `additionalContext`; `sessions-enforce.py` uses `permissionDecision: "deny"` for the one case where it wants to block but exit 0.

**Uncaught-exception failure direction** (decision, h-fix-daic-enforcement-fail-open): a Python traceback exits 1 — *non-blocking* — so for the `PreToolUse` enforcement hook an uncaught exception SILENTLY disables DAIC / protected-path / frozen-path / branch enforcement for that call. `sessions-enforce.py` therefore enforces a **hybrid** policy:
- **Foreseeable corruption fails CLOSED** at the reader level: the `shared_state` state readers (`check_daic_mode_raw`, `check_workflow_bypass`, `get_task_state`) and local `load_config` degrade to restrictive defaults (`discussion` / not-bypassed / null-task / `DEFAULT_CONFIG`) instead of raising. Each runs `ensure_state_dir()` *inside* its `try`, guards `isinstance(data, dict)` before `.get()`, and catches `(FileNotFoundError, json.JSONDecodeError, ValueError, OSError)` — the same pattern the already-safe `read_subagent_depth` used.
- **Unforeseen bugs fail OPEN, LOUDLY**: the whole executable flow runs inside `def main()`, wrapped `try: main() except Exception as exc: <stderr breadcrumb>; sys.exit(0)`. **exit 2 was rejected for that backstop** — the matcher also covers `Read|Grep|Bash`, so a blocking backstop would brick the session with no way to even diagnose (the rejected alternative). `except Exception` (NOT `BaseException`) lets every deliberate `sys.exit(2)` verdict raise `SystemExit` straight through, so the fail-closed guards (token-bridge secret, protected/frozen-path, DAIC, branch) are not weakened. `post-tool-use.py` and `user-messages.py` got the same canonical `pre-compact.py` stdin guard. Tests: `test/test_sessions_enforce_daic.py` (fail-closed on corrupt state + a null-byte-path backstop case), `test/test_hook_stdin_guards.py`.

### State-lock failure direction (m-fix-posttooluse-lock-failure-resilience)
`_state_lock()` — the cross-process `flock`/`msvcrt` lock over `current_task.json` in `shared_state.py` — can fail to **acquire** on a flock-less filesystem (network/synced drive), on permissions/ownership issues, or on `msvcrt` contention. That is a different failure axis from an uncaught bug, and it splits the same fail-safe-vs-fail-closed way. Before the fix, `post-tool-use.py`'s unguarded end-condition throttle (`increment_counter_file`) re-raised the acquisition `OSError` and killed the PostToolUse hook (exit 1, no message) on **every** Bash call on such a host; `sessions-enforce.py` (PreToolUse) takes no lock, which matched the observed PostToolUse-only symptom.
- **Best-effort consumers degrade**: the throttle counters (`increment_counter_file`/`reset_counter_file`) and the subagent-depth helpers (`increment`/`decrement`/`reset_subagent_depth`, via `_mutate_subagent_depth`) fall back to a single UNLOCKED best-effort write on acquisition failure — a throttle / context counter must never break a hook. An **`entered` sentinel**, set as the last statement in the lock body, separates acquisition failure (degrade) from a post-body release/`f.close()` `OSError` (return the already-persisted value, no double-write / no stale-overwrite). `post-tool-use.py`'s end-condition block also gained a defense-in-depth `try/except Exception: pass`, matching its sibling auto-compact / worklog blocks.
- **Integrity writers stay fail-closed**: `set_daic_mode`/`set_task_state`/`set_protocol_state`/`edit_state` keep raising — protected state must never fail-open.
- **The two fail-SAFE DAIC call sites** (`session-start.py` no-protocol fallback, `user-messages.py` emergency STOP) call `ensure_discussion_mode_best_effort()`, which on a lock failure degrades to an UNLOCKED write of only the RESTRICTIVE `"discussion"` mode — safe because it can only tighten enforcement, never open a hole (unlike a general `set_daic_mode` degrade, which could write `"implementation"`). This closes the fail-open where a stale valid `"implementation"` `daic-mode.json` left a STOP / no-protocol session silently ineffective while the banner claimed "all tools locked". Tests: `test/test_post_tool_use_lock_resilience.py`.

## sessions-enforce.py (PreToolUse)

The gatekeeper. It receives `tool_name` and `tool_input`, then walks an ordered series of checks, exiting early as soon as one fires. Ordering is load-bearing (`sessions-enforce.py`). The whole walk runs inside `def main()` under the fail-open backstop described in the exit-code contract above (all helper defs + `PROTECTED_PATHS`/`BASH_WRITE_PATTERNS`/`_BASH_WRITE_TARGET_RULES` stay module-level):

1. **Workflow bypass** — if `check_workflow_bypass()` is true, `sys.exit(0)` immediately, skipping all enforcement.
2. **Protected-path enforcement** — runs *before* the subagent bypass, so even subagents cannot touch `.claude/state/current_task.json`, `daic-mode.json`, `protocol-logs/`, `optimize-state.json`, or `team-management/protocol-configs/system`. `Read`/`Grep` are checked then fast-exit 0 (a performance shortcut so the common read path never reaches the expensive logic below). Edit-family and Bash get both a protected-path block and an optimize frozen-path block.
3. **Bash read-only fast exit** — if a Bash command matches no `BASH_WRITE_PATTERNS` and every pipeline segment matches a configured read-only prefix on a whole-token boundary (`hook_utils.command_is_read_only`), exit 0.
4. **Subagent bypass** — when `in_subagent_context()` is true, subagents bypass DAIC/branch enforcement entirely, except they are still blocked from writing under `.claude/state`.
5. **Administrative whitelist** — task `.md` files under `team-management/tasks/`, anything under `~/.claude/`, and the entire `wiki/` tree are allowed in any DAIC mode so task creation and wiki edits work in discussion mode.
6. **DAIC mode enforcement** — documentation-mode doc-only gating, discussion-mode tool blocking. Policy detail in [DAIC Enforcement](pages/topics/daic-enforcement.md).
7. **Branch enforcement** — verifies the git branch matches the active task's expected branch, with four distinct failure messages and submodule-aware logic.

The frozen-path machinery (`_load_frozen_paths`, `is_frozen_path`, `_bash_targets_frozen`) is consumed here but belongs to the [Optimize Protocols](pages/protocols/optimize-protocols.md) feature.

## session-start.py (SessionStart)

Fires on `startup`/`clear`. It builds a `context` string and returns it via `hookSpecificOutput.additionalContext` — this is how the model receives task and protocol state at the top of a session. Key responsibilities, in order:

- Resolves `developer_name` from config, checks `tiktoken` availability.- **Clears per-session flag files**: context-warning 80/90 flags, `auto-compact-triggered.flag`, `compact-pending.flag`, and `ai-providers-migration-warned.flag` — so warnings re-fire fresh each session.
- **Resets the subagent depth counter** via `reset_subagent_depth()` and unlinks the legacy boolean `in_subagent_context.flag` — self-heals a counter left non-zero by a crashed subagent.
- Emits a one-time deprecation warning for legacy AI-provider config keys; values are never auto-forwarded.
- Loads active-task state and the task file body into context.
- **Protocol state injection + DAIC restore**: if a protocol is active, it reads the current step from the protocol config, calls `set_daic_mode(mode)` to restore the step's DAIC mode, resolves `@`-refs in the step start text, and injects step instructions plus a `[RESUME POINT]` block. If no protocol is active it injects the protocol-first reminder. **The DAIC mode written here is authoritative for resume** — if no protocol set it, the hook defaults the mode to `discussion`.
- **Deploys the task `TEMPLATE.md`** the protocols reference: `ensure_task_template_deployed` (in `shared_state.py`) byte-copies the plugin's `templates/TEMPLATE.md` to `team-management/tasks/TEMPLATE.md` when absent, gated on `team-management/` already existing so an un-opted-in project is never scaffolded. The `config_update` MCP tool also deploys it (so a fresh `/team-management:config` has it without a restart), closing the same-session gap a session-start-only self-heal would leave. Create-if-absent (never clobbers a user-edited copy), atomic (tmp + `os.replace`), best-effort.
- **Deploys + wires the behavioral guidance** (plugin mode, same `team-management/`-exists gate, h-durable-guidance-via-claude-md): rather than injecting `CLAUDE.tm.md` / `CLAUDE.tm.custom.md` / `CLAUDE.wiki.md` into `additionalContext` (the former model — a one-shot injection that faded out of long/`/compact`-ed sessions, leaving the wiki guidance non-recallable mid-session), it calls `shared_state.ensure_guidance_deployed_and_wired(PROJECT_ROOT, plugin_root, wiki_enabled)`. `deploy_guidance_files` refresh-on-change byte-copies the plugin-owned `CLAUDE.tm.md` plus the three knowledge files (`tdd-discipline`/`debugging`/`receiving-feedback` → `team-management/knowledge/`, and `CLAUDE.wiki.md` when wiki is enabled) into the project; `ensure_claude_md_managed_block` then wires an idempotent `<!-- team-management:begin … -->`/`<!-- team-management:end -->` block in the project `CLAUDE.md` holding `@CLAUDE.tm.md` + `@CLAUDE.tm.custom.md` (+ `@CLAUDE.wiki.md`). A project-root `CLAUDE.md` and its `@`-imports are re-read after `/compact` (durable), unlike a SessionStart injection. Deploy runs BEFORE wiring (the wrapper enforces it) so no `@`-target ever dangles, and the `config_update` MCP tool runs the same wrapper for same-session effect. The `CLAUDE.tm.md` knowledge references are backticked (not `@`-imported) so the recursive importer never pulls the knowledge tree into every session; the managed block collapses duplicate/orphan markers and preserves user content.

## post-tool-use.py (PostToolUse)

Runs after every tool completes. It accumulates a `mod` flag; if any reminder was emitted, it exits 2 at the end to surface stderr to Claude. Responsibilities:

- **Workflow-bypass early return**.
- **Subagent depth decrement + transcript archival**: when a `Task`/`Agent` tool completes and we are in subagent context, it calls `decrement_subagent_depth()` *before* the transcript copy (so the depth is corrected even if copy raises), then copies staged transcript chunks from `.claude/state/{subagent_type}/{key}/` into the task's `transcripts/` dir and `rmtree`s the staging dir.
- **Auto-compact token monitoring**: throttled (every 5th tool call, or immediately on `Task`/`Agent`/`Edit`/`Write`/`MultiEdit`). Reads the transcript, computes token usage vs. the model's context limit, and at the configured `auto_compact.threshold` (default 85%) prints a **MANDATORY** directive to run the compaction protocol, guarded by `auto-compact-triggered.flag` so it fires once per session. See [Context Preservation](pages/topics/context-preservation.md).
- **Protocol end-condition injection**: throttled — injects on the first tool call after a protocol-state change and every 10th call after. The throttle uses `protocol-end-condition-counter.txt`; this counter is deleted as a side effect by `set_protocol_state`/`clear_protocol_state`, which is the *only* reset mechanism (the hook has no step-name tracking of its own).
- **Auto work-log append**: during the `implementation` step, every 8th significant tool call (`Edit`/`Write`/`Bash`), appends a `- [date] [auto] <Tool>: <value>` line inside the task file's `## Work Log` section. `_format_worklog_value` collapses multi-line Bash to one line; `_insert_worklog_entry` inserts inside the section (skipping fenced code blocks) rather than at EOF.
- **Provider auto-sync** (`try_provider_sync`): on a task-file edit, if a provider is configured with `auto_sync` enabled and the task is linked, syncs status to GitLab/Jira/GitHub. See [Issue Tracking Providers](pages/subsystems/issue-tracking-providers.md). All failures are swallowed.

## user-messages.py (UserPromptSubmit)

Fires on every prompt submit, before Claude processes it. Builds `context` and returns it via `additionalContext`. Order:

- **Subagent depth reset** — `reset_subagent_depth()` runs *before* the bypass check, because bypass is the long-lived mode where a stale counter would otherwise accumulate.
- **Workflow-bypass early return** — still injects the `[[ ultrathink ]]` + bypass-active banner.
- Prepends `[[ ultrathink ]]` unless `api_mode` is set.
- **Post-compact restoration**: if `compact-pending.flag` exists (written by `pre-compact.py`), reads the checkpoint, unlinks the flag, and injects a `[POST-COMPACT RESTORATION]` block (task/branch/mode/protocol/services).
- **Token monitoring**: the fallback path when auto-compact is disabled — 80%/90% warnings guarded by per-threshold flag files, plus the same auto-compact directive (also flag-guarded).
- **Read-only task-state hints** and the **protocol-required reminder** injected when no protocol is active.
- **Auto save-note during investigation**: persists the user's prompt (truncated to 500 chars) into the protocol log's `notes` array for session recovery.
- **Trigger-phrase / pattern detection**:
  - Emergency stop — `SILENCE` or `STOP` (case-sensitive) forces global discussion mode via `set_daic_mode(True)`. **Deliberately transient** (decided in m-hooks-hygiene-sweep, N7): the next session-start restores the active protocol step's mode, so the stop lasts until session restart — protocol state stays the single owner of DAIC mode across sessions, and no separate stop-flag exists. The injected message says so explicitly ("until session restart").
  - `iterloop` injects iterate-over-a-list instructions.
  - Protocol-intent phrase detection (substring match on `prompt_lower`): compaction, task-completion, task-creation, task-switching. Each appends the shared `_PROTOCOL_EXPLAIN_RULE` plus a `[Detected protocol: ...]` hint — it does **not** start the protocol, only nudges the model to explain and ask first.
  - Task-detection regex patterns (gated by `task_detection.enabled`) inject a "this may be a task" notice.

## pre-compact.py (PreCompact)

Fires on `auto` or `manual` compaction triggers. It writes a checkpoint to `.claude/state/compact-pending.flag` capturing `trigger`, `task`, `branch`, `daic_mode`, `protocol` state, and `services`. It then clears the context-warning and auto-compact flags so they re-trigger after compaction, prints an informational stderr line, and exits 0 — `PreCompact` cannot block, so there is nothing to deny. Its `json.load(sys.stdin)` and the protocol status line are both **guarded** (m-enforcement-and-git-hardening): empty/malformed stdin degrades to `{}` and a non-int `current_step` is `isinstance`-guarded, because the harness treats ANY non-zero PreCompact exit as a hook failure that errors out `/compact`. The matching restoration happens in `user-messages.py` on the next prompt.

## task-transcript-link.py (PreToolUse, Task|Agent)

Fires when a subagent is dispatched. It **increments the subagent depth counter** (skipped under workflow bypass to stay symmetric with the decrement), then chunks the parent transcript into ~18k-token batches and stages them under `.claude/state/{subagent_type}/{invocation_key}/` for the subagent to read.

**Bounded staging read** (m-fix-unbounded-transcript-reads): the parent transcript is read via `shared_state._read_file_tail(transcript_path, TRANSCRIPT_STAGE_CAP_BYTES, return_capped=True)` — only the last 1 MB, so this blocking `PreToolUse` hook's tiktoken-encode + RSS stay flat as the session JSONL grows to tens of MB. The pre-work-removal strip loop (which drops entries up to the first `Edit`/`Write`/`MultiEdit` marker) runs only when the read was NOT `capped`: on a capped read the marker may sit before the window, so retaining the bounded tail verbatim over-includes recent context rather than emptying it and starving the subagent. The `_read_file_tail` import is hoisted above the depth increment (an `ImportError` then fails before the increment, non-blocking) and an unreadable transcript degrades to empty staging + `exit 0` instead of blocking the Task. `get_model_from_transcript` / `get_context_length_from_transcript` / `read_last_jsonl_entry` (see [Context Preservation](pages/topics/context-preservation.md)) share the same `_scan_tail_then_full` tail-read primitive.

The `subagent_type` and `invocation_key` are derived from the hook's own `tool_input` so they match what `post-tool-use.py` recomputes for archival. See [Specialized Agents](pages/entities/specialized-agents.md) for how subagents consume these chunks.

**Exception-safety around the increment** (h-fix-discard-clean-and-windows-transcript): the increment happens *before* the chunking, so a crash in chunking must not orphan it — an orphaned increment leaves `in_subagent_context()` true, and the main agent silently bypasses DAIC for the rest of the turn. The chunk-file writes use `encoding='utf-8'` (they previously omitted it and crashed on Windows cp1252 with a non-ASCII transcript char), and the entire post-increment body is wrapped in `try/except Exception`: on any failure it decrements (undoing its own increment) and `sys.exit(2)` to BLOCK the Task. Blocking is load-bearing — a blocked Task means `post-tool-use.py` never runs its matching decrement, so the counter cannot be double-decremented (a plain decrement + `exit 0` would let the Task proceed and get decremented a second time, corrupting a parallel sibling). `except Exception` (not `BaseException`) deliberately lets the normal `sys.exit(0)` early-returns pass through, keeping the increment when the Task proceeds.

## Cross-Hook Shared State

All hooks import from `shared_state.py`, which owns the state files and the durable-write primitive (`_write_json_durable`) — a tempfile + `os.replace` + fsync so readers never see a torn write (this fixed a stale-read race). Key shared mechanisms relevant to the hooks:

- **Subagent depth counter** (`.claude/state/subagent-depth.json`): `read_subagent_depth()` is lock-free and tolerant (missing/corrupt/negative → 0); `increment`/`decrement`/`reset` mutate under `_state_lock()` via `_mutate_subagent_depth`, which **degrades to an unlocked best-effort write on a lock-acquisition failure** (see State-lock failure direction above). `in_subagent_context()` is `depth > 0`. The counter (vs. a boolean flag) is what keeps parallel subagents correctly detected — the first to finish decrements N→N-1, leaving siblings still protected.
- **DAIC mode** (`daic-mode.json`): read by `check_daic_mode_raw()` (fail-closed default `"discussion"`), written by `set_daic_mode()` (integrity writer — raises on a lock failure) and, for the two fail-SAFE restrictive-direction call sites (session-start no-protocol fallback, emergency STOP), by `ensure_discussion_mode_best_effort()` (degrades to an unlocked `"discussion"` write on a lock failure).
- **Protocol/task state** (`current_task.json`): read via `get_task_state()` / `get_protocol_state()`. Note `set_protocol_state`/`clear_protocol_state` delete the post-tool-use throttle counter as a side effect — the sole reset for the end-condition throttle.

## Gotchas

- **Both `Task` and `Agent` must be recognized.** Some Claude Code harnesses name the subagent-dispatch tool `Agent`, not `Task`. The increment guard in `task-transcript-link.py` (`tool_name not in ("Task", "Agent")`) and the decrement guard in `post-tool-use.py` must agree, and the `hooks.json` matcher is `Task|Agent` (`plugin/hooks/hooks.json`). If only `Task` were recognized, an `Agent` dispatch would never increment the depth, so `in_subagent_context()` would always return False inside that subagent — silently defeating the DAIC subagent bypass, reminder suppression, auto-worklog gating, and transcript staging.
- **Protected-path enforcement runs before the subagent bypass on purpose.** Subagents are trusted to edit task files but never `.claude/state` — the early protected-path block (`sessions-enforce.py`) plus the in-bypass `.claude/state` re-check enforce both.
- **The legacy `daic`-substring Bash shim was removed (`m-retire-dead-agents-and-daic-cleanup`).** Earlier, discussion-mode `sessions-enforce.py` intercepted any Bash command containing the literal substring `daic` (a leftover from the retired `daic` CLI) and denied it with an "already in discussion mode" message — false-triggering on innocent reads like `grep daic file`, paths such as `tools/daic.py`, or any command carrying `2>/dev/null` (the redirect trips `BASH_WRITE_PATTERNS`, disabling the read-only fast-path). That shim is gone; `daic-mode.json` remains write-protected by the independent `check_bash_protected` PROTECTED_PATHS gate. Regression test: `test/test_sessions_enforce_daic.py`.
- **Throttle counters are reset only by protocol-state writers.** The post-tool-use end-condition counter has no step-name awareness; it relies entirely on `set_protocol_state`/`clear_protocol_state` deleting the counter file. Any protocol-state mutation (start, advance, goto, abort, loop iteration, resume) gives the next tool call a fresh inject.
- **Auto-compact has two trigger paths** (post-tool-use mid-turn, user-messages at prompt time), both guarded by the same `auto-compact-triggered.flag` so the directive fires at most once per session. The flag is cleared by `pre-compact.py` and `session-start.py`.
- **SessionStart's DAIC write is authoritative.** On resume it overwrites `daic-mode.json` from the active protocol step's mode (or `discussion` if none). A manual mode set before a session restart will not survive.
- **`SILENCE`/`STOP` detection is case-sensitive and whole-word** (`shared_state.is_emergency_stop`, `_EMERGENCY_STOP_RE = re.compile(r'\b(?:STOP|SILENCE)\b')`) — the word boundaries mean substrings like `BACKSTOP` / `unSTOPpable` do NOT trigger, but any prompt containing the whole word `STOP` (or `SILENCE`) forces discussion mode. Observed repeatedly in the field: a background-agent completion notification whose text contained the token `STOP` (e.g. review output discussing "emergency STOP") flips a live session to discussion mid-protocol, because the harness feeds notification text through the `UserPromptSubmit` hook. The stop is transient by design (see Emergency stop above); resync to the protocol step's mode via the MCP mode-switch tool or a session restart.
- **The `sessions-enforce.py` matcher must list every tool the hook special-cases.** The hook only fires for tools in its `PreToolUse` matcher, so any tool it compares `tool_name` against but that is absent from the matcher gets *no enforcement at all* — the hook never runs for it. This is how `NotebookEdit` silently escaped DAIC/protected-path/frozen-path/branch enforcement until h-fix-mcp-manager-split-and-notebookedit added it. The full special-cased set is `{Read, Grep, Edit, Write, MultiEdit, NotebookEdit, Bash}`. `test/test_hooks_matcher_drift.py` guards this by AST-deriving every literal compared against `tool_name` (plus `DEFAULT_CONFIG["blocked_tools"]`) and asserting the matcher covers all of them — so the next tool added to a gate can't silently miss the matcher.
