---
title: Context Preservation
tags: [hooks, config]
created: 2026-05-31
updated: 2026-07-14
sources: [plugin/hooks/pre-compact.py, plugin/hooks/post-tool-use.py, plugin/hooks/user-messages.py, plugin/hooks/session-start.py, plugin/hooks/shared_state.py]
---

# Context Preservation

The framework survives two distinct events that would otherwise destroy working state: **context-window compaction** (Claude Code drops old turns to free tokens) and **session restart** (a new context window starts cold). The mechanism is a chain of hooks plus the [Hooks System](pages/subsystems/hooks-system.md) that (a) proactively trigger a structured compaction *before* the model is forced into a lossy native compact, (b) checkpoint task/branch/protocol/DAIC state to disk on any native compact, and (c) re-inject that state into the next turn. Config keys live in `auto_compact.*` (see [Configuration Schema](pages/entities/configuration-schema.md)).

There are three independent token-usage trips, all reading from the same source, plus the PreCompact checkpoint that catches whatever the trips miss.

## Token counting (the shared source)

All percentage math goes through three helpers in `shared_state.py`:

- `get_context_length_from_transcript(path)` (`shared_state.py`) returns the newest main-chain `usage` total (`input_tokens + cache_read_input_tokens + cache_creation_input_tokens`), skipping `isSidechain` (subagent) entries. **This is NOT tiktoken** — the count comes straight from the Claude API usage record already in the transcript. **Bounded tail-read** (m-statusline-and-test-infra): it does NOT parse the whole JSONL — the transcript grows to tens of MB, so it reverse-scans only the last `TRANSCRIPT_TAIL_BYTES` (1 MB) via the shared `_scan_tail_then_full` primitive (the transcript is append-ordered, so the last eligible entry in file order is the newest — no timestamp comparison). It falls back to ONE full-file scan only when the bounded tail holds no usage entry AND the file exceeds the window (a single >1 MB final tool-result/paste pushed the newest usage outside it) — so the common path is flat w.r.t. session length and there is no auto-compact blind spot. The sibling `read_last_jsonl_entry(path)` (last-entry timestamp/sessionId for the statusline) shares the same tail-then-full primitive.
- `get_model_from_transcript(path)` (`shared_state.py`) returns the newest `message.model`, or `"unknown"`. **Bounded tail-read** (m-fix-unbounded-transcript-reads): like its two sibling readers it reverse-scans only the last `TRANSCRIPT_TAIL_BYTES` (1 MB) via `_scan_tail_then_full` (it was the last reader to escape the m-statusline-and-test-infra sweep — an unbounded `readlines()` over the whole file that then inspected only the final 10 entries). It now finds the model even when it sits >10 entries back, and falls back to one full-file scan only when the bounded tail holds no `message.model` AND the file exceeds the window.
- `get_model_context_limit(display, id)` (`shared_state.py`) picks the limit: `auto_compact.context_limit` config override first, else a regex on the model string (`[1m]`, `(1M context)`, `1M context` → `1000000`), else default `200000`.

Percentage is `context_length / limit * 100`. tiktoken *is* imported (session-start.py checks for it, user-messages.py soft-imports it) but only for subagent transcript chunking elsewhere — the auto-compact / warning math does not use it.

## Mechanism 1 — Auto-compact trip in post-tool-use (the primary path)

`post-tool-use.py` monitors token usage after tool calls and injects a mandatory compaction directive at the threshold.

1. Reads `auto_compact.enabled` (default `True`) and `auto_compact.threshold` (default `85`) from `team-management/config.json` (`post-tool-use.py`).
2. Gated on `_ac_enabled and not in_subagent`, and on the once-per-session flag `.claude/state/auto-compact-triggered.flag` not existing (`post-tool-use.py`).
3. **Throttle** (`post-tool-use.py`): a counter in `auto-compact-counter.txt` only lets the expensive transcript read happen every 5th call — OR immediately on a significant tool (`Task`, `Agent`, `Edit`, `Write`, `MultiEdit`).
4. On a check, computes the percentage via the three helpers above. If `pct >= threshold`, prints a `[AUTO-COMPACT: …]` stderr block ordering the model to consolidate the work log via the `logging` agent + `protocol_save_note`, then run native `/compact` (the PreCompact hook auto-preserves state), and do nothing else first; sets `mod = True` (→ `sys.exit(2)`, feeding stderr back to Claude) and touches the once-per-session flag (`post-tool-use.py`).

## Mechanism 2 — Auto-compact trip in user-messages (turn-boundary path)

`user-messages.py` runs the same auto-compact logic at the start of each user turn (the `[AUTO-COMPACT…]` directive at `user-messages.py`, same default `threshold=85`, same `auto-compact-triggered.flag` guard). This catches a session that crosses the threshold between tool calls. Both mechanisms target the same flag, so the directive fires once per session regardless of which hook trips first.

## Mechanism 3 — 80% / 90% warning fallback (only when auto-compact is disabled)

`user-messages.py`. These are *fallbacks*, structured as `if pct >= 90 … elif pct >= 80 …` and gated on per-session flags `context-warning-90.flag` / `context-warning-80.flag` (`user-messages.py`):

- `>= 90%` → `[90% WARNING] … CRITICAL: consolidate then run /compact`.
- `>= 80%` → `[80% WARNING] … context is getting low`.

When auto-compact is enabled and the threshold is the default 85, the auto-compact directive fires first; the 90% warning still serves as a louder backstop if compaction was skipped. (See Gotchas for the 80/90 vs 75/90 split.)

## Mechanism 4 — PreCompact checkpoint (catches native compaction)

`pre-compact.py` fires on Claude Code's `PreCompact` event for both `manual` (`/compact`) and `auto` (window-full) triggers. Native compaction is lossy, so this hook snapshots the recoverable state:

1. Gathers `task` + `branch` from `get_task_state()`, `daic_mode` from `check_daic_mode_raw()`, and `protocol` from `get_protocol_state()` (`pre-compact.py`).
2. Writes the checkpoint dict to `.claude/state/compact-pending.flag` as JSON (`pre-compact.py`).
3. Clears `context-warning-80.flag`, `context-warning-90.flag`, and `auto-compact-triggered.flag` so all three trips can re-arm in the post-compact session (`pre-compact.py`).
4. Prints a `[PreCompact]` status to stderr and **exits 0** (`pre-compact.py`).

The `exit 0` is load-bearing: `PreCompact` has no blocking semantics, and the harness treats any non-zero PreCompact exit as a hook failure that errors out `/compact`. The checkpoint write completes before the exit, so 0 is the correct success signal even though every other enforcement hook signals "feed stderr to Claude" with exit 2.

## Mechanism 5 — Post-compact restoration

`user-messages.py`, the first thing each turn after the bypass early-return. If `compact-pending.flag` exists, a compaction just completed:

1. Reads and `unlink()`s the flag (`user-messages.py`).
2. Injects a `[POST-COMPACT RESTORATION]` block listing the checkpointed task, branch, DAIC mode, protocol step, and services (`user-messages.py`) into `additionalContext`, telling the model to resume from the work log.

Note: restoration runs in **user-messages.py**, not session-start.py. A native `/compact` does not restart the session, so the next event is a user prompt — that is where the flag is consumed. session-start.py only handles a genuine session restart.

## Session-restart restoration (session-start.py)

On a fresh session, `session-start.py` rebuilds context independently of the compact flag:

- Clears stale `context-warning-80/90.flag` and `auto-compact-triggered.flag` (`session-start.py`) so a new session starts un-armed. **`compact-pending.flag` is cleared only when the SessionStart `source != "resume"`** (m-enforcement-and-git-hardening): the SessionStart matcher now includes `resume`, so session-start fires on `claude --resume` — a resume continues the same logical session, so clearing the not-yet-consumed post-compact checkpoint there would erase it before `user-messages.py` restores it. Every flag-clear routes through a `_safe_unlink` that swallows `OSError` (a locked/undeletable flag must not kill SessionStart).
- Resets the subagent-depth counter (`session-start.py`) — a session that crashed mid-subagent would otherwise leave depth > 0 and silently bypass DAIC ([DAIC Enforcement](pages/topics/daic-enforcement.md)).
- Loads the active task file and work log into `additionalContext`, and re-injects the active protocol step's start/end text and DAIC mode via the [Protocol Engine](pages/subsystems/protocol-engine.md) (`session-start.py`). This `[RESUME POINT]` block is what carries task continuity across a restart.

## Graceful compaction (what the model does once triggered)

There is no `context-compaction.md` runbook any more — it was retired in `h-retire-legacy-protocol-pointers` (it was never deployed and told the model to shell-write PROTECTED `.claude/state/` paths the hooks block). The auto-compact / 90% directives now steer the model toward a *graceful* compaction before the 100% forced native compact: (1) consolidate the work log via the `logging` agent, (2) save open findings via `protocol_save_note`, then (3) run native `/compact`. State preservation is automatic — the **PreCompact hook** (`pre-compact.py`) writes a checkpoint (task / branch / protocol / DAIC mode) and clears the context-warning flags before the native compaction, and post-compact restoration (`user-messages.py`) re-injects the session summary afterward.

## Design decisions

- **Trigger before the cliff.** Auto-compact fires at 85% so the structured protocol (with agent-driven log consolidation) runs while there is still room, rather than letting the harness force a lossy native compact at 100%. The 90% warning is the backstop if 85% was skipped.
- **API usage records, not tiktoken.** Counting from transcript `usage` fields is exact (it is what the API actually billed) and cheap, avoiding a tiktoken encode of the whole transcript on every check. tiktoken is reserved for chunking subagent transcripts.
- **Once-per-session flags.** Every trip is single-shot per session (`auto-compact-triggered.flag`, `context-warning-*.flag`) to avoid nagging the model on every subsequent tool call once over threshold. PreCompact clears them so they re-arm after a compaction.
- **Two checkpoint layers.** PreCompact's `compact-pending.flag` survives a same-session native compact; session-start's task/protocol re-injection survives a full restart. They are deliberately separate: the compact case has no restart event to hook.
- **Throttled monitoring.** The 5-call counter keeps the transcript-read cost off the hot path while still catching threshold crossings promptly on significant tools.

## Gotchas

- **Warnings use the global 80/90 flags.** The runtime warning trips use `context-warning-80.flag` / `context-warning-90.flag` (user-messages.py; cleared in pre-compact.py and session-start.py). The former *task-scoped* 75/90 flag family (`TaskStateManager.set_context_warning` / `clear_context_warnings`) was removed with the multi-session scaffold in l-dead-code-removal, so only the global 80/90 path remains. Some older docstrings/comments still say "75%"; the live code path emits at 80%.
- **Restoration is consumed by user-messages, not session-start.** If you add restoration logic to session-start.py expecting it to fire after `/compact`, it will not — `/compact` does not restart the session. session-start.py even *clears* `compact-pending.flag`, so a restore must already have happened by the time a new session begins.
- **Threshold reading is best-effort.** Both auto-compact blocks wrap config reads in bare `try/except` and default `enabled=True`, `threshold=85`. A malformed `config.json` silently falls back to defaults rather than disabling the feature.
- **Subagent suppression.** Auto-compact monitoring is gated on `not in_subagent` (post-tool-use.py). A subagent burning tokens does not trip the main-session compaction; only main-chain `usage` counts (`isSidechain` entries are skipped in `get_context_length_from_transcript`).
- **PreCompact must exit 0.** Returning 2 (the convention everywhere else) makes the harness treat `/compact` as failed. This is regression-guarded.
- **Counter files are unprotected.** `auto-compact-counter.txt` and the warning flags live in `.claude/state/` but are plain files; a manual delete just re-arms the trip. The protected state files are `current_task.json` / `daic-mode.json` / protocol logs (see [State Files](pages/entities/state-files.md)), not these throttle counters.
