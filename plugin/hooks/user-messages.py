#!/usr/bin/env python3
"""User message hook for protocol enforcement, context monitoring, and special patterns."""
import json
import sys
import re
import os
from datetime import datetime
try:
    import tiktoken
except ImportError:
    tiktoken = None
from shared_state import (
    ensure_discussion_mode_best_effort, check_workflow_bypass,
    PROTOCOL_LOGS_DIR, _write_json_durable,
    reset_subagent_depth,
    is_emergency_stop, detect_protocol_intent,
)

# Load input — guard against empty / malformed / non-dict stdin. An unguarded
# json.load on empty stdin raises, and the harness treats a hook traceback as a
# failure. Degrade to {} (h-fix-daic-enforcement-fail-open).
try:
    input_data = json.load(sys.stdin)
except (json.JSONDecodeError, ValueError):
    input_data = {}
if not isinstance(input_data, dict):
    input_data = {}
prompt = input_data.get("prompt", "")
transcript_path = input_data.get("transcript_path", "")
context = ""

# Get configuration (if exists)
try:
    from shared_state import get_project_root
    PROJECT_ROOT = get_project_root()
    CONFIG_FILE = PROJECT_ROOT / "team-management" / "config.json"
    
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
    else:
        config = {}
except Exception as e:
    print(f"[DAIC] Warning: Failed to load config: {e}", file=sys.stderr)
    config = {}

# ============================================================================
# SUBAGENT DEPTH RESET (turn boundary)
# ============================================================================
# A new user prompt means the previous turn fully completed and the main agent
# is unambiguously back in control — no subagent (Task) can still be running.
# Reset the depth counter to 0 here, BEFORE the workflow-bypass early-return,
# because bypass is exactly the long-lived mode where a stale counter would
# otherwise accumulate and survive into normal operation.
try:
    reset_subagent_depth()
except Exception:
    pass

# ============================================================================
# WORKFLOW BYPASS CHECK
# ============================================================================
# If workflow bypass is enabled, skip all processing.
if check_workflow_bypass():
    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "[[ ultrathink ]]\n\n[WORKFLOW BYPASS ACTIVE] All DAIC enforcement is bypassed.\n"
        }
    }
    print(json.dumps(output))
    sys.exit(0)

# Check API mode and add ultrathink if not in API mode
if not config.get("api_mode", False):
    context = "[[ ultrathink ]]\n"

# ============================================================================
# POST-COMPACT RESTORATION
# ============================================================================
# If compact-pending.flag exists, a compaction just completed.
# Inject task context summary to restore working state.
try:
    compact_pending_flag = PROJECT_ROOT / ".claude" / "state" / "compact-pending.flag"
    if compact_pending_flag.exists():
        checkpoint = json.loads(compact_pending_flag.read_text(encoding='utf-8'))
        compact_pending_flag.unlink()

        cp_task = checkpoint.get("task")
        cp_branch = checkpoint.get("branch")
        cp_mode = checkpoint.get("daic_mode", "discussion")
        cp_protocol = checkpoint.get("protocol")
        cp_services = checkpoint.get("services", [])

        context += "\n[POST-COMPACT RESTORATION] Context was just compacted. Restoring session state:\n"
        if cp_task:
            context += f"- Task: {cp_task}\n"
        if cp_branch:
            context += f"- Branch: {cp_branch}\n"
        context += f"- DAIC Mode: {cp_mode}\n"
        if cp_protocol:
            context += f"- Protocol: {cp_protocol.get('name')} (step {cp_protocol.get('current_step', 0) + 1}: {cp_protocol.get('step_name', 'unknown')})\n"
        if cp_services:
            context += f"- Services: {', '.join(cp_services)}\n"
        context += "Review the task file and work log to continue from where you left off.\n"
except Exception:
    pass

# Token monitoring (using shared utilities)
from shared_state import get_context_length_from_transcript, get_model_from_transcript, get_model_context_limit

if transcript_path and os.path.exists(transcript_path):
    context_length = get_context_length_from_transcript(transcript_path)

    if context_length > 0:
        # Get model name and determine context limit
        model_name = get_model_from_transcript(transcript_path)
        context_limit = get_model_context_limit(model_name, model_name)

        # Calculate percentage of usable context
        usable_percentage = (context_length / context_limit) * 100

        # Check for warning flag files to avoid repeating warnings
        # Use task-scoped warnings if task is active, otherwise global flags
        from shared_state import get_task_state
        PROJECT_ROOT = get_project_root()
        task_state = get_task_state()
        current_task = task_state.get("task")

        # Global flag files (legacy support)
        warning_80_flag = PROJECT_ROOT / ".claude" / "state" / "context-warning-80.flag"
        warning_90_flag = PROJECT_ROOT / ".claude" / "state" / "context-warning-90.flag"

        # Check if warning was already shown (global flags)
        def warning_shown(threshold):
            flag = warning_80_flag if threshold == 80 else warning_90_flag
            return flag.exists()

        def mark_warning_shown(threshold):
            flag = warning_80_flag if threshold == 80 else warning_90_flag
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.touch()

        # Format context limit for display (e.g., "160k" or "1M")
        limit_display = f"{context_limit // 1000}k" if context_limit < 1000000 else "1M"

        # Auto-compact configuration
        auto_compact_enabled = config.get("auto_compact", {}).get("enabled", True)
        auto_compact_threshold = config.get("auto_compact", {}).get("threshold", 85)
        auto_compact_flag = PROJECT_ROOT / ".claude" / "state" / "auto-compact-triggered.flag"

        # Auto-compact: mandatory compaction directive at threshold
        if auto_compact_enabled and usable_percentage >= auto_compact_threshold and not auto_compact_flag.exists():
            context += (
                f"\n[AUTO-COMPACT: {context_length:,}/{limit_display} tokens ({usable_percentage:.1f}%)] "
                f"Context usage has reached {auto_compact_threshold}% threshold.\n"
                f"**MANDATORY**: Compact now — delegate to the `logging` agent to consolidate the work log, "
                f"save any open findings via `protocol_save_note`, then run `/compact`. "
                f"The PreCompact hook preserves your task, branch, protocol, and DAIC state automatically. "
                f"Do NOT continue other work until compaction is complete.\n"
            )
            # Write flag so this only triggers once per session
            auto_compact_flag.parent.mkdir(parents=True, exist_ok=True)
            auto_compact_flag.touch()

        # Token warnings (only show once per session) — fallback when auto-compact disabled
        if usable_percentage >= 90 and not warning_shown(90):
            context += f"\n[90% WARNING] {context_length:,}/{limit_display} tokens used ({usable_percentage:.1f}%). CRITICAL: consolidate via the `logging` agent + `protocol_save_note`, then run `/compact` (the PreCompact hook auto-preserves task/branch/protocol/DAIC state).\n"
            mark_warning_shown(90)
        elif usable_percentage >= 80 and not warning_shown(80):
            context += f"\n[80% WARNING] {context_length:,}/{limit_display} tokens used ({usable_percentage:.1f}%). Context is getting low. Be aware of coming context compaction trigger.\n"
            mark_warning_shown(80)

# ============================================================================
# READ-ONLY TASK STATE HINTS
# ============================================================================
# Auto-detection and mode switching moved to protocol engine + MCP tools.
# This section only provides read-only hints.
try:
    from shared_state import get_task_state
    import subprocess

    task_state = get_task_state()
    current_task = task_state.get("task")

    if not current_task:
        # No active task — provide hint if on a branch
        try:
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                capture_output=True, text=True, timeout=5, check=False
            )
            current_branch = result.stdout.strip() if result.returncode == 0 else None
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            current_branch = None

        if current_branch and current_branch not in ("main", "master"):
            context += (
                f"[No active task, but on branch '{current_branch}'. "
                f"Start a protocol: mcp__plugin_team-management_tm__protocol_start(protocol_name=\"task\").]\n"
            )
except Exception:
    pass

# ============================================================================
# PROTOCOL ENFORCEMENT
# ============================================================================
# When protocol engine is enabled but no protocol is active, inject a
# persistent reminder on EVERY user message. This ensures the AI always
# sees the instruction to start a protocol before implementing code.
try:
    protocol_engine_enabled = True
    tm_config_file = PROJECT_ROOT / "team-management" / "config.json"
    if tm_config_file.exists():
        with open(tm_config_file, 'r', encoding='utf-8') as f:
            full_config = json.load(f)
        protocol_engine_enabled = full_config.get("protocol_engine", {}).get("enabled", True)

    if protocol_engine_enabled:
        from shared_state import get_protocol_state
        protocol_info = get_protocol_state()
        if not protocol_info:
            context += (
                "\n[PROTOCOL REQUIRED] No workflow protocol is active. "
                "Before starting any protocol, you MUST: "
                "(1) Discuss the user's request and understand what they need, "
                "(2) Explain WHICH protocol you recommend and WHY, "
                "(3) Get explicit user approval, "
                "(4) Only then call mcp__plugin_team-management_tm__protocol_start(). "
                "Do NOT skip the explanation step — never call protocol_start without telling the user which protocol you chose and why.\n"
            )
except Exception:
    pass

# ============================================================================
# AUTO SAVE_NOTE DURING INVESTIGATION
# ============================================================================
# When protocol is active on "investigation" step, automatically save the
# user's message as a protocol note to preserve discussion context for
# session recovery after clear/restart.
try:
    from shared_state import get_protocol_state, get_task_state as _get_task_state_note
    _protocol_info = get_protocol_state()
    if _protocol_info and _protocol_info.get("step_name") == "investigation" and prompt.strip():
        _note_task_state = _get_task_state_note()
        _note_task = _note_task_state.get("task")
        # Determine log file (task-named or _pending)
        _log_file = None
        if _note_task:
            _log_file = PROTOCOL_LOGS_DIR / f"{_note_task}.json"
        if not _log_file or not _log_file.exists():
            _log_file = PROTOCOL_LOGS_DIR / "_pending.json"
        if _log_file.exists():
            _log_data = {}
            try:
                with open(_log_file, "r", encoding="utf-8") as _f:
                    _log_data = json.load(_f)
            except (json.JSONDecodeError, IOError):
                _log_data = {"steps": [], "gotos": [], "notes": []}
            if "notes" not in _log_data:
                _log_data["notes"] = []
            # Truncate long messages to 500 chars
            _note_text = prompt.strip()
            if len(_note_text) > 500:
                _note_text = _note_text[:500] + "..."
            _log_data["notes"].append({
                "at": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "step": "investigation",
                "note": f"[auto] User: {_note_text}",
            })
            try:
                _write_json_durable(_log_file, _log_data, ensure_ascii=False)
            except (IOError, OSError):
                pass
except Exception:
    pass

# Emergency stop (works in any mode)
# Deliberately transient: this writes the global DAIC mode only. The next
# session-start restores the active protocol step's mode (session-start.py),
# so the stop lasts until session restart. No separate stop flag — protocol
# state stays the single owner of DAIC mode across sessions; restarting the
# session is itself an explicit user action that ends the emergency.
if is_emergency_stop(prompt):  # whole-word, case-sensitive (shared_state)
    # Fail-safe force of discussion mode (m-fix-posttooluse-lock-failure-resilience):
    # a _state_lock acquisition failure on the STOP path must not crash
    # UserPromptSubmit, AND the STOP must still take effect — otherwise the banner
    # would claim "all tools locked" while a stale "implementation" mode kept edits
    # allowed. ensure_discussion_mode_best_effort degrades to an UNLOCKED write of
    # the RESTRICTIVE discussion mode on lock failure (never raises, only tightens),
    # so the emergency STOP is honoured even on a flock-less FS. set_daic_mode itself
    # is unchanged (integrity writer, still raises).
    ensure_discussion_mode_best_effort()  # force global discussion mode
    context += "[DAIC: EMERGENCY STOP] All tools locked. You are now in discussion mode (until session restart — the active protocol step's mode is restored then). Re-align with your pair programmer.\n"

# Iterloop detection
if "iterloop" in prompt.lower():
    context += "You have been instructed to iteratively loop over a list. Identify what list the user is referring to, then follow this loop: present one item, wait for the user to respond with questions and discussion points, only continue to the next item when the user explicitly says 'continue' or something similar\n"

# Protocol detection — map a user directive to the live protocol mechanism.
# De-noised via shared_state.detect_protocol_intent (word-boundary, present-tense
# anchoring), so ordinary report text no longer misfires the way the old
# substring matchers did. Protocols are engine-driven (MCP protocol_* tools /
# native /compact) — there is no protocol markdown file to read.
_PROTOCOL_EXPLAIN_RULE = (
    "\n[PROTOCOL EXPLANATION REQUIRED] Before starting ANY protocol, you MUST:\n"
    "1. Tell the user WHICH protocol you detected and WHY (what triggered it)\n"
    "2. Briefly explain what the protocol does\n"
    "3. Ask for explicit permission to proceed\n"
    "4. Only start the protocol (via the MCP protocol_* tools) AFTER the user approves\n"
    "Do NOT skip this step. Protocols are engine-driven — there is no protocol file to read.\n"
)

_PROTOCOL_INTENT_HINTS = {
    "context-compaction": (
        "[Detected: context compaction] The message suggests compacting context. "
        "Explain that you'd run `/compact` (the PreCompact hook preserves task/branch/"
        "protocol/DAIC state automatically) and ask for approval first.\n"
    ),
    "task-completion": (
        "[Detected: task completion] The message suggests completing a task. "
        "Explain that you'd advance the active task protocol to its completion step "
        "(`protocol_advance`) and ask for approval first.\n"
    ),
    "task-creation": (
        "[Detected: task creation] The message suggests creating a task. "
        "Explain that you'd start the task protocol "
        "(`protocol_start(protocol_name=\"task\")` — its investigation step defines the task) "
        "and ask for approval first.\n"
    ),
    "task-startup": (
        "[Detected: task startup] The message suggests switching/resuming a task. "
        "Explain that you'd start the task protocol "
        "(`protocol_start(protocol_name=\"task\", task=\"<name>\")`) and ask for approval first.\n"
    ),
}

_protocol_intent = detect_protocol_intent(prompt)
if _protocol_intent:
    context += _PROTOCOL_EXPLAIN_RULE
    context += _PROTOCOL_INTENT_HINTS[_protocol_intent]

# Task detection patterns (optional feature)
if config.get("task_detection", {}).get("enabled", True):
    task_patterns = [
        r"(?i)we (should|need to|have to) (implement|fix|refactor|migrate|test|research)",
        r"(?i)create a task for",
        r"(?i)add this to the (task list|todo|backlog)",
        r"(?i)we'll (need to|have to) (do|handle|address) (this|that) later",
        r"(?i)that's a separate (task|issue|problem)",
        r"(?i)file this as a (bug|task|issue)"
    ]
    
    task_mentioned = any(re.search(pattern, prompt) for pattern in task_patterns)
    
    if task_mentioned:
        # Add task detection note
        context += """
[Task Detection Notice]
The message may reference something that could be a task.

IF you or the user have discovered a potential task that is sufficiently unrelated to the current task, ask if they'd like to create a task file.

Tasks are:
• More than a couple commands to complete
• Semantically distinct units of work
• Work that takes meaningful context
• Single focused goals (not bundled multiple goals)
• Things that would take multiple days should be broken down
• NOT subtasks of current work (those go in the current task file/directory)

If they want to create a task, follow the task creation protocol.
"""

# Output the context additions
if context:
    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context
        }
    }
    print(json.dumps(output))

sys.exit(0)
