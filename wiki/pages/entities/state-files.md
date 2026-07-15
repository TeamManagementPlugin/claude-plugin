---
title: State Files
tags: [config, hooks, daic]
created: 2026-05-31
updated: 2026-07-14
sources: [plugin/hooks/shared_state.py, plugin/hooks/task_state_manager.py, plugin/hooks/sessions-enforce.py, plugin/hooks/session-start.py, plugin/hooks/issue_provider_base.py]
---

# State Files

All runtime state lives under `.claude/state/`. These files are the durable memory of the framework: which task is active, which DAIC mode is in force, how deep the subagent stack is, which files an optimize run has frozen, and how Claude tasks map to provider issues. Hooks (which run as short-lived standalone processes) and the MCP server are separate processes that read and write the *same* files, so the central design problem this module solves is **cross-process durability and atomicity** — a hook must never read a half-written file, and two concurrent writers must never clobber each other. `shared_state.py` is the single source of truth for the read/write helpers; `task_state_manager.py` manages the per-task state directory (`tasks/<task>/transcripts/` staging + cleanup on completion — the former multi-session scaffold was removed in l-dead-code-removal).

## File Catalog

Paths are relative to `.claude/state/`. "PROTECTED" means a hook (`sessions-enforce.py`) blocks direct agent edits — see [DAIC Enforcement](pages/topics/daic-enforcement.md).

| File | Holds | Writer(s) | Protected? |
|------|-------|-----------|------------|
| `current_task.json` | Active task: `task`, `branch`, `services`, `updated`, and the embedded `protocol` block (`name`, `current_step`, `step_name`, `started_at`, plus optimize extras `loop_iteration` / `experimentation_started_at`) | `shared_state.set_task_state`; protocol block via `set_protocol_state` / `clear_protocol_state` (MCP-only) | Yes |
| `daic-mode.json` | `{"mode": "discussion"|"implementation"|"documentation"}` | `set_daic_mode` / `edit_state` (both write under `_state_lock`; the dead `check_daic_mode_bool` / `check_daic_mode` / `toggle_daic_mode` were deleted in m-hooks-hygiene-sweep) | Yes |
| `optimize-state.json` | Optimize-protocol frozen-path list + baseline metrics (`baseline_metric`, `baseline_wall_clock_s`, `baseline_commit`) | `write_optimize_state` only | Yes |
| `provider-tokens.json` | Per-project, user-authored provider tokens keyed by provider name (`{"gitlab": "<token>", …}`; legacy `CLAUDE_PLUGIN_OPTION_<KEY>` keys still read as fallback); 0600; git-ignored; the AI cannot read it | `ensure_provider_tokens_file` (SessionStart + `config_update`; create-if-absent, never clobbers/deletes) | Yes (secret) |
| `protocol-logs/<task>.json` | Per-task audit log (step transitions, gotos, aborts, notes, `SPEC_REVIEW: PASSED` sentinels) | `ProtocolEngine` audit log (MCP-only); `_pending.json` before task known | Yes (dir) |
| `subagent-depth.json` | `{"depth": N}` — subagent (Task) nesting depth | `increment` / `decrement` / `reset_subagent_depth` | No |
| `workflow-bypass.json` | `{"enabled", "reason", "updated"}` — global DAIC bypass | `set_workflow_bypass` | No |
| `current_task.lock` | Empty lock file for the cross-process write lock | `_state_lock()` (opened, never JSON) | No |
| `gitlab-mappings.json` | Task → GitLab issue mapping + sync metadata | `IssueTrackingTaskSync._locked_mapping_update` (locked RMW; `_save_mappings` under `_state_lock`) | No |
| `jira-mappings.json` | Task → Jira issue mapping + sync metadata | same | No |
| `github-mappings.json` | Task → GitHub/Gitea issue mapping + sync metadata | same | No |
| `tasks/<task>/transcripts/` | Per-task subagent transcript archives | hooks (`post-tool-use.py`) | No |
| `{subagent_type}/{key}/` | Per-Task-invocation transcript staging (sanitised type + hash key) | `task-transcript-link.py` (staged), `post-tool-use.py` (archived then `rmtree`) | No |
| `ai-providers-migration-warned.flag` | One-time legacy-AI-key deprecation warning fired | `session-start.py` | No |
| `compact-pending.flag` / `auto-compact-triggered.flag` | Compaction checkpoint / monitoring markers | `pre-compact.py`; cleared by `session-start.py` | No |
| `protocol-end-condition-counter.txt` | Single-int throttle for post-tool-use end-condition reminders | `post-tool-use.py`; deleted by `set_protocol_state` / `clear_protocol_state` | No |

`optimize-state.json` and `tasks/<task>/` directories are **absent on non-optimize / idle projects** — their absence is a deliberate zero-cost fast path (see Gotchas). The three `*-mappings.json` files only exist once a provider is configured and a task is linked; a `"disabled"`-provider project never creates them.

## Mechanics

### Durable write: tempfile + fsync + atomic rename

Every JSON write funnels through `_write_json_durable(path, data, **kwargs)` (`shared_state.py`). It writes to a **unique temp file** in the target directory via `tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")`, calls `f.flush()` then `os.fsync(f.fileno())`, and only then `os.replace(tmp, path)`; on any write failure the temp file is unlinked and the exception re-raised. `os.replace` is atomic on both POSIX and Windows, so a reader always observes either the complete old content or the complete new content — never a truncated file.

The unique temp name (m-hooks-hygiene-sweep, R2-5) matters for files NOT serialised by `_state_lock`: with the old deterministic `f"{path}.tmp"`, two concurrent writers of e.g. `workflow-bypass.json` shared one temp file — `os.replace` kept the final file consistent, but the loser's update was silently dropped. `mkstemp` is unique across processes AND threads. Side effects: resulting state files are 0600 (mkstemp perms survive the rename; acceptable — same-user consumers, consistent with the deliberate 0600 on `config.json`), and a SIGKILL between mkstemp and replace leaks one uniquely-named `.tmp` (benign clutter, accepted).

The `fsync` is the load-bearing part. Without it, the rename could complete while the new bytes were still in the OS page cache, and a hook spawned milliseconds later (hooks are separate processes) could read stale/empty content — the **state-sync race**. `fsync` forces the bytes to disk before the rename, closing that window. This is the canonical writer for *all* state JSON: DAIC mode, task state, subagent depth, optimize state, protocol logs, and provider mappings (`issue_provider_base.py` routes `_save_mappings` through it too).

### Cross-process write lock

`current_task.json` is read-modify-write (callers preserve sibling fields), so concurrent writers from different processes could lose updates. `_state_lock()` (`shared_state.py` Windows / POSIX) is a `@contextmanager` that takes an exclusive lock on `current_task.lock` — `fcntl.flock(LOCK_EX)` on POSIX, `msvcrt.locking(LK_LOCK)` on Windows. Every mutator of `current_task.json` runs **lock → `get_task_state()` → modify → `_write_json_durable`** inside the lock: `set_task_state`, `set_protocol_state`, `clear_protocol_state`. The subagent-depth mutators reuse the same lock.

Since m-hooks-hygiene-sweep (R2-4) the lock also serialises `daic-mode.json` writers — `set_daic_mode` and `edit_state` take it around their `_write_json_durable` call, closing the lose-update window between session-start's mode restore and a concurrent MCP mode switch (session-start's no-protocol fallback now routes through `set_daic_mode` for the same reason). The same sweep added locked counter helpers `increment_counter_file(path) -> int` / `reset_counter_file(path)` (utf-8; missing/corrupt file counts as 0) used by all three post-tool-use throttle counters (R2-6).

Since m-fix-mappings-lock-free-rmw the lock ALSO serialises the provider **mappings** files (`gitlab/jira/github-mappings.json`) — they too are read-modify-write and were written from both the PostToolUse auto-sync hook and the MCP server. All 14 mapping mutation sites route through `IssueTrackingTaskSync._locked_mapping_update(mutator)`, which wraps `_load_mappings → mutator → _save_mappings` in the same `_state_lock`. Its degrade model is deliberately **stricter** than the counter helpers (which use a single `entered` sentinel and swallow their own write errors): `_save_mappings` is a durable integrity write, so a **two-marker** (`acquired`/`completed`) scheme lets it distinguish a lock-acquisition failure (→ one unlocked best-effort RMW) from an `OSError` inside the held lock (→ propagate, never fall back to an unlocked write) from a post-persist release failure (→ return the persisted result). A corrupt-file `RuntimeError` propagates unchanged. `_ensure_mappings_file` was likewise hardened from a check-then-write TOCTOU to an atomic create-only `open(path, 'x')` seed (companioned by `_load_mappings` treating a 0-byte file as `{}`).

Critical ordering constraint: `read_subagent_depth()` (`shared_state.py`) is **intentionally lock-free**. The depth mutators call it *inside* the `_state_lock()` critical section; if `read_subagent_depth` re-acquired the (non-reentrant) lock it would deadlock (`shared_state.py`).

### `set_protocol_state` / `clear_protocol_state` (MCP-only)

These write the `protocol` sub-object of `current_task.json` and are explicitly fenced as **MCP-ONLY — never call from hooks** (`shared_state.py`). The four canonical fields (`name`, `current_step`, `step_name`, `started_at`) are always overwritten; an optional `extra: dict` is merged on top (`shared_state.py`) — the optimize protocols persist `loop_iteration` and `experimentation_started_at` this way without a parameter-channel change for non-looping protocols. Both functions, as a deliberate side effect inside the same lock, `unlink` `protocol-end-condition-counter.txt` (`shared_state.py`) so the post-tool-use throttle resets on every protocol-state change. See [Protocol Engine](pages/subsystems/protocol-engine.md).

### `write_optimize_state`

`write_optimize_state(state)` (`shared_state.py`) is the **only** writer of `optimize-state.json`. It does `ensure_state_dir()` then `_write_json_durable(OPTIMIZE_STATE_FILE, state)` — no `_state_lock` (single logical writer, the protocol engine). The docstring marks it T1-owned for the frozen-paths workstream; the engine reaches it through this public helper exclusively rather than touching the file directly. See [Optimize Protocols](pages/protocols/optimize-protocols.md).

### Subagent depth counter

`subagent-depth.json` replaced a legacy boolean `in_subagent_context.flag` that broke under parallel subagents (the first sibling to finish cleared the flag while others were still running). The counter is incremented on Task/Agent PreToolUse, decremented (clamped at 0) on PostToolUse, and hard-reset to 0 at every turn/session boundary (`UserPromptSubmit`, `SessionStart`) — the main agent is unambiguously in control there, so 0 self-heals any leak from an interrupted subagent (`shared_state.py`). `in_subagent_context()` (depth > 0) gates DAIC suppression and reminder/auto-sync injection. See [Specialized Agents](pages/entities/specialized-agents.md) and [Hooks System](pages/subsystems/hooks-system.md).

### Per-task state directories (`task_state_manager.py`)

`TaskStateManager` owns `tasks/<task>/`. Its live surface is three methods (the multi-session scaffold — `session.json` CRUD, parallel-session detection, per-task context-warning flags, `get_task_by_branch`, and a duplicate module-level `get_task_state_manager` — was removed in l-dead-code-removal):
- `get_task_state_dir` resolves the path and raises `ValueError` on an empty/whitespace name, any name resolving to the `tasks/` root itself (`.`, `./`, `..`-spellings — via a `task_dir == tasks_root` check), and `../` path traversal (`relative_to`). This closes the hazard where a bad name could make `cleanup_task_state` `rmtree` the whole tasks tree.
- `get_transcripts_dir` returns `tasks/<task>/transcripts/` (subagent transcript staging, consumed by `post-tool-use.py`).
- `cleanup_task_state` `rmtree`s the directory on completion; returns `False` (never raises) for a bad/empty name or a missing dir, and swallows `PermissionError`/`OSError` because Windows may hold file locks. The engine reaches it via `shared_state.cleanup_task_state_on_completion` → `shared_state.get_task_state_manager` (the surviving factory).

## Design Decisions

- **One durable writer, used everywhere.** Centralising on `_write_json_durable` means the state-sync race fix applies uniformly — there is no second hand-rolled `json.dump` path that could regress. Provider mappings deliberately route through it (`issue_provider_base.py`).
- **Lock the read-modify-write files.** `current_task.json`, `daic-mode.json`, the throttle counters, and (since m-fix-mappings-lock-free-rmw) the provider `*-mappings.json` files all have genuine multi-writer RMW contention (hooks and MCP both touch them) and take `_state_lock`. `optimize-state.json` is last-writer-wins single-writer (the protocol engine) and skips the lock to stay cheap. `_state_lock` is one global lock, not per-file — coarser but simpler; because it is non-reentrant, no locked RMW path may call another (e.g. a mapping mutation must never run inside a `set_protocol_state` critical section).
- **Protect via hook, not filesystem perms.** `current_task.json`, `daic-mode.json`, `protocol-logs/`, `optimize-state.json`, `provider-tokens.json`, and `team-management/protocol-configs/system/` are listed in `PROTECTED_PATHS` (`sessions-enforce.py`); `is_protected_path` blocks Read/Grep/Edit/Write and write-flavoured Bash against them, with `_collapse_redundant_segments` collapsing `/./` and `//` spelling variants first. For most files the intent is to force all state changes through the MCP tools / engine so the audit log and lock discipline are never bypassed. `provider-tokens.json` is additionally a **secret** (per-project user-authored provider tokens), so a dedicated `_targets_token_bridge` guard keeps it blocked even under workflow bypass — `os.path.normpath` + component-boundary for structured-path tools (Read/Grep/Edit/Write), best-effort contiguous-literal for Bash (split/`..`-mid-command/variable/`$()`/`python -c` forms are the irreducible shell residual; the real protections are 0600 + gitignore + scoping/rotation).
- **Absence as a fast path.** `_load_frozen_paths` returns `[]` immediately when `optimize-state.json` is absent (`sessions-enforce.py`), so non-optimize projects pay zero enforcement overhead. The same "file does not exist → default" idiom appears in every reader (`get_task_state`, `check_daic_mode_raw`, `read_subagent_depth`).
- **Tolerant readers.** Every read helper catches `FileNotFoundError`/`json.JSONDecodeError` and returns a safe default — e.g. `check_daic_mode_raw` (`shared_state.py`) returns `discussion` on a missing or corrupt file, so a fresh project defaults to the safe (read-only) mode. (It does **not** create the file; the earlier `check_daic_mode_bool`, which did, was deleted in m-hooks-hygiene-sweep.)

## Gotchas

- **`daic-mode.json` IS protected.** Despite the table in the root CLAUDE.md historically omitting it, the live `PROTECTED_PATHS` list at `sessions-enforce.py` includes `.claude/state/daic-mode.json`. Switch modes via the protocol engine / `daic_mode_switch_*` MCP tools, never by editing the file.
- **`set_protocol_state` from a hook will corrupt the throttle and bypass the audit trail.** It is MCP-only (`shared_state.py`). Hooks may only call the read functions (`get_protocol_state`, `get_protocol_log`, `load_protocol_config`).
- **`read_subagent_depth` must stay lock-free.** It is called *inside* `_state_lock()` by the increment/decrement mutators; making it acquire the lock would deadlock (non-reentrant lock) — see the explicit warning at `shared_state.py`.
- **A leaked subagent depth self-heals only at a turn/session boundary.** If a Task is hard-interrupted between increment and decrement, the counter stays elevated until the next `UserPromptSubmit`/`SessionStart` reset — mid-turn it can mis-suppress reminders.
- **`current_task.lock` is never JSON.** It is opened purely for `flock`/`msvcrt` locking. Do not read it as state.
- **Stale `optimize-state.json` does not block unrelated work.** `_load_frozen_paths` also returns `[]` when no protocol is active or the active step is not `experimentation` (`sessions-enforce.py`), so an abandoned optimize file from an aborted run will not freeze edits in a later, unrelated session. `protocol_abort` intentionally leaves `optimize-state.json` on disk for forensic salvage.- **Provider mappings are global, not task-scoped.** `gitlab/jira/github-mappings.json` live at the `.claude/state/` root (`task_state_manager.py` docstring), keyed by task name. They survive `cleanup_task_state` (which only removes `tasks/<task>/`).
