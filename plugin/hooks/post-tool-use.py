#!/usr/bin/env python3
"""Post-tool-use hook: protocol end-condition injection, auto-compact monitoring, auto-worklog appends, and multi-provider auto-sync."""
import json
import shutil
import sys
from shared_state import (
    check_daic_mode_raw, get_project_root, get_task_state,
    check_workflow_bypass, get_task_state_manager,
    in_subagent_context, decrement_subagent_depth, subagent_transcript_key,
    subagent_dir_name, parse_task_frontmatter,
    increment_counter_file, reset_counter_file
)
from hook_utils import is_task_markdown_path

# Load input — guard against empty / malformed / non-dict stdin. An unguarded
# json.load on empty stdin raises, and the harness treats a hook traceback as a
# failure. Degrade to {} (h-fix-daic-enforcement-fail-open). NOTE: only the stdin
# parse is hardened here — the _state_lock hot-path resilience is the separate
# m-fix-posttooluse-lock-failure-resilience task.
try:
    input_data = json.load(sys.stdin)
except (json.JSONDecodeError, ValueError):
    input_data = {}
if not isinstance(input_data, dict):
    input_data = {}
tool_name = input_data.get("tool_name", "")
tool_input = input_data.get("tool_input", {})
cwd = input_data.get("cwd", "")
mod = False

# ============================================================================
# WORKFLOW BYPASS CHECK
# ============================================================================
# If workflow bypass is enabled, skip all end-condition/reminder injection and provider syncing.
if check_workflow_bypass():
    sys.exit(0)

# Check if we're in a subagent context (depth counter > 0)
project_root = get_project_root()
in_subagent = in_subagent_context()

# If this is the subagent-dispatch tool completing, decrement the depth and copy
# transcripts. The tool is named "Task" in some Claude Code harnesses and "Agent"
# in others; recognise both so the depth counter stays balanced everywhere
# (task-transcript-link.py increments on the matching PreToolUse).
if tool_name in ("Task", "Agent") and in_subagent:
    # Decrement BEFORE the transcript-copy block (mirrors the old unlink
    # placement) so the depth is corrected even if the copy raises. Each
    # parallel Task decrements only its own increment, so still-running siblings
    # keep the depth > 0 and stay protected from reminder/DAIC injection.
    decrement_subagent_depth()

    # Copy transcripts to task-scoped directory if task is active
    try:
        task_state = get_task_state()
        current_task = task_state.get("task")
        subagent_type = subagent_dir_name(tool_input)
        # Same per-invocation key task-transcript-link.py staged under — tool_input
        # is identical across the Pre/Post payloads, so this resolves to that Task's
        # own keyed dir even when parallel same-type subagents are in flight.
        invocation_key = subagent_transcript_key(tool_input)

        if current_task and subagent_type:
            # Source: .claude/state/{subagent_type}/{key}/
            source_dir = project_root / '.claude' / 'state' / subagent_type / invocation_key

            if source_dir.exists():
                # Destination: .claude/state/tasks/{task}/transcripts/{subagent_type}/{key}/
                state_manager = get_task_state_manager()
                task_transcripts_dir = state_manager.get_transcripts_dir(current_task)
                dest_dir = task_transcripts_dir / subagent_type / invocation_key
                dest_dir.mkdir(parents=True, exist_ok=True)

                # Copy all transcript files
                for src_file in source_dir.glob("current_transcript_*.json"):
                    dest_file = dest_dir / src_file.name
                    shutil.copy2(src_file, dest_file)

                # Remove the keyed staging dir after archiving so per-invocation
                # dirs don't accumulate in .claude/state/.
                shutil.rmtree(source_dir, ignore_errors=True)
    except Exception:
        # Don't fail the hook if transcript copying fails
        pass

    # Suppress end-condition / reminder injection for the Task-completion tool call
    in_subagent = True

# Check current DAIC mode from global state
task_state = get_task_state()
current_task = task_state.get("task")
discussion_mode = (check_daic_mode_raw() == "discussion")

# ============================================================================
# AUTO-COMPACT TOKEN MONITORING
# ============================================================================
# Throttled check: every 5th tool call, read transcript and check token usage.
# At configured threshold, inject mandatory compaction directive.
# Uses flag file to only trigger once per session.
try:
    _ac_config_file = project_root / "team-management" / "config.json"
    _ac_enabled = True
    _ac_threshold = 85
    if _ac_config_file.exists():
        with open(_ac_config_file, 'r', encoding='utf-8') as f:
            _ac_config = json.load(f)
        _ac_enabled = _ac_config.get("auto_compact", {}).get("enabled", True)
        _ac_threshold = _ac_config.get("auto_compact", {}).get("threshold", 85)

    if _ac_enabled and not in_subagent:
        _ac_flag = project_root / '.claude' / 'state' / 'auto-compact-triggered.flag'
        if not _ac_flag.exists():
            # Throttle: only check every 5th tool call. Locked increment
            # (R2-6) — concurrent PostToolUse hooks must not lose-update.
            _ac_counter_file = project_root / '.claude' / 'state' / 'auto-compact-counter.txt'
            _ac_counter = increment_counter_file(_ac_counter_file)

            _ac_should_check = (_ac_counter >= 5) or tool_name in {"Task", "Agent", "Edit", "Write", "MultiEdit"}
            if _ac_should_check:
                reset_counter_file(_ac_counter_file)
                transcript_path = input_data.get("transcript_path", "")
                if transcript_path:
                    import os
                    if os.path.exists(transcript_path):
                        from shared_state import (
                            get_context_length_from_transcript,
                            get_model_from_transcript,
                            get_model_context_limit
                        )
                        _ac_ctx_len = get_context_length_from_transcript(transcript_path)
                        if _ac_ctx_len > 0:
                            _ac_model = get_model_from_transcript(transcript_path)
                            _ac_limit = get_model_context_limit(_ac_model, _ac_model)
                            _ac_pct = (_ac_ctx_len / _ac_limit) * 100
                            if _ac_pct >= _ac_threshold:
                                _ac_limit_display = f"{_ac_limit // 1000}k" if _ac_limit < 1000000 else "1M"
                                print(
                                    f"[AUTO-COMPACT: {_ac_ctx_len:,}/{_ac_limit_display} tokens ({_ac_pct:.1f}%)] "
                                    f"Context usage has reached {_ac_threshold}% threshold. "
                                    f"**MANDATORY**: Compact now — consolidate the work log (logging agent) "
                                    f"+ protocol_save_note, then run /compact. The PreCompact hook preserves "
                                    f"task/branch/protocol/DAIC state automatically. "
                                    f"Do NOT continue other work until compaction is complete.",
                                    file=sys.stderr
                                )
                                mod = True
                                # Write flag so this only triggers once per session
                                _ac_flag.parent.mkdir(parents=True, exist_ok=True)
                                _ac_flag.touch()

except Exception:
    pass

# ============================================================================
# PROTOCOL END CONDITION INJECTION
# ============================================================================
# If a protocol is active, inject the current step's end condition.
# Throttled: only every 10th tool call, or on the first call after a
# protocol state change (the engine deletes the counter file on changes).
protocol_enabled = True
try:
    tm_config_file = project_root / "team-management" / "config.json"
    if tm_config_file.exists():
        import json as _json
        with open(tm_config_file, 'r', encoding='utf-8') as f:
            _tm_config = _json.load(f)
        protocol_enabled = _tm_config.get("protocol_engine", {}).get("enabled", True)
except Exception:
    pass

def get_protocol_end_condition():
    """Read protocol state and return end condition for current step."""
    try:
        task_state_data = get_task_state()
        protocol_info = task_state_data.get("protocol")
        if not protocol_info:
            return None

        protocol_name = protocol_info.get("name")
        step_name = protocol_info.get("step_name")
        current_step_idx = protocol_info.get("current_step", 0)

        from shared_state import load_protocol_config
        protocol_config = load_protocol_config(protocol_name)
        if not protocol_config:
            return None

        steps = protocol_config.get("steps", [])
        if current_step_idx < len(steps):
            step = steps[current_step_idx]
            end_text = step.get("end", "")
            total = len(steps)
            return (
                f'[Protocol "{protocol_name}" step {current_step_idx + 1}/{total}: '
                f'"{step_name}"] Completion condition: {end_text}'
            )
    except Exception:
        pass
    return None

if protocol_enabled and not in_subagent:
    # Throttle: inject on first call after a protocol state change (counter
    # file absent — set_protocol_state / clear_protocol_state delete it) and
    # then every 10th tool call within the same protocol step. Locked
    # increment (R2-6): deleted file → returns 1 → (1-1) % 10 == 0 → inject.
    # Exception-guarded like the sibling auto-compact (101-153) and worklog
    # (297-329) blocks so any residual failure degrades to "no injection this
    # call" instead of crashing the hook. increment_counter_file already
    # degrades on lock-acquisition failure (m-fix-posttooluse-lock-failure-
    # resilience); this outer guard is defense-in-depth for anything else in
    # the block (e.g. get_protocol_end_condition).
    try:
        _throttle_file = project_root / '.claude' / 'state' / 'protocol-end-condition-counter.txt'
        _counter = increment_counter_file(_throttle_file)

        if (_counter - 1) % 10 == 0:
            end_condition = get_protocol_end_condition()
            if end_condition:
                print(end_condition, file=sys.stderr)
                mod = True
    except Exception:
        pass

def _format_worklog_value(tool_name, tool_input):
    """Single-line value for an auto work-log entry.

    Bash has no file_path and the command may be multi-line (heredocs, if/fi,
    provider wrappers); collapse every whitespace run to one space so the entry
    stays one markdown list line, and keep the HEAD (the command verb + first
    args are the readable part). Edit/Write log a file_path and keep the TAIL so
    a long path's filename basename survives truncation.
    """
    if tool_name == "Bash":
        value = " ".join(tool_input.get("command", "").split())
        if len(value) > 80:
            value = value[:77] + "..."
        return value
    value = tool_input.get("file_path", "")
    if len(value) > 80:
        value = "..." + value[-77:]
    return value


def _insert_worklog_entry(content, entry):
    """Insert entry at the end of the ## Work Log section.

    Appends inside the section (before the next level-1/level-2 ATX heading)
    rather than at end-of-file, so an entry lands under Work Log even when a
    later section such as "# Code Review" follows. Nested ###/#### subheadings
    (the logging-agent consolidated format) are NOT treated as the boundary, so
    entries stay in chronological order at the section end. Fenced code blocks
    are skipped so a '#'-comment inside a logged command is not mistaken for a
    heading. The section is created at EOF when absent.
    """
    lines = content.split("\n")
    wl_idx = None
    in_fence = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        # Skip a "## Work Log" line inside a fenced code block (e.g. a markdown
        # example earlier in the task file) — only the real section heading counts.
        if not in_fence and line.strip() == "## Work Log":
            wl_idx = i
            break
    if wl_idx is None:
        return content.rstrip() + f"\n\n## Work Log\n{entry}\n"

    end_idx = len(lines)
    in_fence = False
    for j in range(wl_idx + 1, len(lines)):
        stripped = lines[j].lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        hashes = len(stripped) - len(stripped.lstrip("#"))
        if 1 <= hashes <= 2 and stripped[hashes:hashes + 1] == " ":
            end_idx = j
            break

    # Sit immediately after the last content line of Work Log: skip back over
    # blank lines padding the section tail.
    insert_at = end_idx
    while insert_at > wl_idx + 1 and lines[insert_at - 1].strip() == "":
        insert_at -= 1

    lines.insert(insert_at, entry)
    result = "\n".join(lines)
    if not result.endswith("\n"):
        result += "\n"
    return result


# ============================================================================
# AUTO WORK LOG UPDATE DURING IMPLEMENTATION
# ============================================================================
# When protocol is on "implementation" step, auto-append brief entries to
# the task work log after significant tool calls (Edit, Write, Bash).
# This preserves progress context for session recovery.
if protocol_enabled and not in_subagent and tool_name in {"Edit", "Write", "Bash"}:
    try:
        from shared_state import get_protocol_state as _get_ps
        _ps = _get_ps()
        if _ps and _ps.get("step_name") == "implementation" and current_task:
            # Throttle: use a separate counter, only log every 8th significant
            # tool call. Locked increment (R2-6).
            _worklog_counter_file = project_root / '.claude' / 'state' / 'worklog-auto-counter.txt'
            _wl_counter = increment_counter_file(_worklog_counter_file)

            if _wl_counter >= 8:
                reset_counter_file(_worklog_counter_file)
                # Find task file and append work log entry
                _task_file = None
                _base = project_root / "team-management"
                _tf = _base / "tasks" / f"{current_task}.md"
                if not _tf.exists():
                    _tf = _base / "tasks" / current_task / "README.md"
                if _tf.exists():
                    _task_file = _tf

                if _task_file:
                    try:
                        import datetime as _dt
                        _content = _task_file.read_text(encoding="utf-8")
                        _today = _dt.date.today().strftime("%Y-%m-%d")
                        _value = _format_worklog_value(tool_name, tool_input)
                        _entry = f"- [{_today}] [auto] {tool_name}: {_value}"
                        _content = _insert_worklog_entry(_content, _entry)
                        _task_file.write_text(_content, encoding="utf-8")
                    except Exception:
                        pass
    except Exception:
        pass

implementation_tools = ["Edit", "Write", "MultiEdit", "NotebookEdit"]

# Check for cd command in Bash operations
if tool_name == "Bash":
    command = tool_input.get("command", "")
    if "cd " in command:
        print(f"[CWD: {cwd}]", file=sys.stderr)
        mod = True

# Multi-Provider Integration - Auto-sync on task file modifications
def try_provider_sync():
    """Attempt to sync task status to active provider if configured and linked"""
    try:
        # Only sync if we're not in a subagent and we modified a task file
        if in_subagent:
            return

        # Check if this was a task file edit
        task_file_modified = False
        if tool_name in implementation_tools:
            file_path = tool_input.get("file_path", "")
            # is_task_markdown_path slash-normalizes first, so a Windows
            # backslash path (team-management\tasks\x.md) is still detected —
            # the bare substring check was silently dead on Windows.
            if is_task_markdown_path(file_path):
                task_file_modified = True

        if not task_file_modified:
            return

        # Get current task
        task_state = get_task_state()
        current_task = task_state.get("task")
        if not current_task:
            return

        # Load config to detect provider
        config_file = project_root / 'team-management' / 'config.json'
        if not config_file.exists():
            return

        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)

        # Detect active provider
        provider = config.get('issue_tracking', {}).get('provider', 'disabled')

        if provider == 'disabled':
            return

        # Check if auto-sync enabled for this provider
        provider_config = config.get(provider, {})
        if not provider_config.get('auto_sync', False):
            return

        # Import and use provider-specific sync
        try:
            if provider == 'gitlab':
                from gitlab_utils import get_gitlab_sync
                sync = get_gitlab_sync()
                if not sync:
                    return

                # Check if task linked to GitLab
                mapping = sync.get_task_mapping(current_task)
                if not mapping:
                    return

                # Read task status from frontmatter and sync.
                # Keep the file-existence early-return + 'pending' default to
                # preserve exact pre-refactor behavior (an existing task file with
                # missing/malformed frontmatter still syncs 'pending', not skip).
                task_file = project_root / 'team-management' / 'tasks' / f'{current_task}.md'
                if not task_file.exists():
                    return
                status = parse_task_frontmatter(current_task).get('status', 'pending')

                # Sync to GitLab
                sync.sync_task_status_to_gitlab(current_task, status)

            elif provider == 'jira':
                from jira_utils import get_jira_sync
                sync = get_jira_sync()
                if not sync:
                    return

                # Check if task linked to Jira
                mapping = sync.get_task_mapping(current_task)
                if not mapping:
                    return

                # Read task status from frontmatter and sync.
                # Keep the file-existence early-return + 'pending' default to
                # preserve exact pre-refactor behavior (an existing task file with
                # missing/malformed frontmatter still syncs 'pending', not skip).
                task_file = project_root / 'team-management' / 'tasks' / f'{current_task}.md'
                if not task_file.exists():
                    return
                status = parse_task_frontmatter(current_task).get('status', 'pending')

                # Sync to Jira
                sync.sync_task_status_to_issue(current_task, status)

            elif provider == 'github':
                from github_utils import get_github_sync
                sync = get_github_sync()
                if not sync:
                    return

                # Check if task linked to GitHub
                mapping = sync.get_task_mapping(current_task)
                if not mapping:
                    return

                # Read task status from frontmatter and sync.
                # Keep the file-existence early-return + 'pending' default to
                # preserve exact pre-refactor behavior (an existing task file with
                # missing/malformed frontmatter still syncs 'pending', not skip).
                task_file = project_root / 'team-management' / 'tasks' / f'{current_task}.md'
                if not task_file.exists():
                    return
                status = parse_task_frontmatter(current_task).get('status', 'pending')

                # Sync to GitHub
                sync.sync_task_status_to_issue(current_task, status)

        except Exception:
            # Silently fail - don't break the hook for provider issues
            pass

    except Exception:
        # Silently fail - don't break the hook for any issues
        pass

# Attempt provider sync
try_provider_sync()

if mod:
    sys.exit(2)  # Exit code 2 feeds stderr back to Claude
else:
    sys.exit(0)