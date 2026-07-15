#!/usr/bin/env python3
"""PreCompact hook to preserve state before Claude Code's native context compaction.

Fires on both 'manual' (/compact) and 'auto' (context window full) triggers.
Saves a checkpoint of current task, branch, protocol state, and DAIC mode
so that post-compact restoration can inject context back after compaction.
"""
import json
import sys
from shared_state import (
    get_project_root, get_task_state, get_protocol_state,
    check_daic_mode_raw
)

# Load input — guard against empty / malformed stdin. An unguarded json.load
# on empty stdin raises, and the harness treats any non-zero PreCompact exit as
# a hook failure that errors out /compact.
try:
    input_data = json.load(sys.stdin)
except (json.JSONDecodeError, ValueError):
    input_data = {}
if not isinstance(input_data, dict):
    input_data = {}
trigger = input_data.get("trigger", "unknown")

project_root = get_project_root()
state_dir = project_root / '.claude' / 'state'
state_dir.mkdir(parents=True, exist_ok=True)

# Write compact-pending flag with checkpoint info
task_state = get_task_state()
current_task = task_state.get("task")
current_branch = task_state.get("branch")

# Get DAIC mode from global state
daic_mode = check_daic_mode_raw()

# Get protocol state
protocol_info = get_protocol_state()

checkpoint = {
    "trigger": trigger,
    "task": current_task,
    "branch": current_branch,
    "daic_mode": daic_mode,
    "protocol": protocol_info,
    "services": task_state.get("services", [])
}

compact_flag = state_dir / 'compact-pending.flag'
compact_flag.write_text(json.dumps(checkpoint, indent=2), encoding='utf-8')

# Clear context warning flags so they can re-trigger after compaction
for flag_name in ['context-warning-80.flag', 'context-warning-90.flag']:
    flag_file = state_dir / flag_name
    if flag_file.exists():
        flag_file.unlink()

# Clear auto-compact-triggered flag so monitoring can re-trigger after compaction
auto_compact_flag = state_dir / 'auto-compact-triggered.flag'
if auto_compact_flag.exists():
    auto_compact_flag.unlink()

# Show status to user via stderr
task_info = f" (task: {current_task})" if current_task else ""
# Build the protocol status fragment defensively: a malformed protocol block
# may be a non-dict, or carry a non-int current_step — neither must raise here.
if isinstance(protocol_info, dict):
    _p_step = protocol_info.get('current_step', 0)
    _p_step_label = f"{_p_step + 1}" if isinstance(_p_step, int) else str(_p_step)
    protocol_info_str = f", protocol: {protocol_info.get('name', '?')} step {_p_step_label}"
else:
    protocol_info_str = ""
print(f"[PreCompact] Saving checkpoint before {trigger} compaction{task_info}{protocol_info_str}", file=sys.stderr)

sys.exit(0)  # Exit 0: checkpoint saved; PreCompact has nothing to block, stderr message is informational
