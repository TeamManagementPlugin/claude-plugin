# ===== IMPORTS ===== #

## ===== STDLIB ===== ##
import json, sys, subprocess, os
from pathlib import Path
from datetime import datetime, timezone
##-##

## ===== WINDOWS UTF-8 STDOUT FIX ===== ##
# Windows uses cp1252 by default, which can't encode Unicode block characters (█, ░)
# Force UTF-8 encoding for stdout to prevent UnicodeEncodeError
if sys.platform == 'win32':
    import io
    # Reconfigure stdout with UTF-8 encoding
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
##-##

## ===== 3RD-PARTY ===== ##
##-##

## ===== LOCAL ===== ##
# _PLUGIN_IMPORT records whether shared_state was imported from a PLUGIN install
# (env branch OR the __file__ self-locate branch). Consumed later to skip the
# legacy "no hooks in project settings.json == disabled" inference — plugin-mode
# hooks live in the plugin's hooks.json, not the project settings
# (m-fix-plugin-mode-install-bugs).
_PLUGIN_IMPORT = False
_self_hooks = Path(__file__).resolve().parent.parent / "hooks"
if os.environ.get('CLAUDE_PLUGIN_ROOT'):
    # Plugin runtime: hooks live under the plugin install (replaced on update),
    # the project is a separate dir (CLAUDE_PROJECT_DIR). Import shared_state from
    # the plugin's hooks and take PROJECT_ROOT FROM shared_state (env-first ->
    # CLAUDE_PROJECT_DIR) so this script's project root is identical to the one
    # get_protocol_state / load_config / load_protocol_config read internally —
    # no split-root drift between statusline's reads and shared_state's reads.
    _PLUGIN_IMPORT = True
    sys.path.insert(0, str(Path(os.environ['CLAUDE_PLUGIN_ROOT']).resolve() / "hooks"))
    from shared_state import edit_state, Model, Mode, find_git_repo, load_state, IconStyle, PROJECT_ROOT, load_config, get_model_context_limit, get_context_length_from_transcript, read_last_jsonl_entry, check_workflow_bypass, get_protocol_state, load_protocol_config
elif Path(__file__).resolve().parent.name == "templates" and (_self_hooks / "shared_state.py").exists():
    # Plugin install, but the host did NOT inject CLAUDE_PLUGIN_ROOT — this is how
    # Claude Code invokes a settings.json statusLine command. This script ships at
    # <plugin>/templates/statusline.py, so its sibling ../hooks is the plugin's
    # hooks dir. Self-locate it and take PROJECT_ROOT from shared_state (env-first
    # -> CLAUDE_PROJECT_DIR -> cwd walk), identical to the env branch above. The
    # parent.name == "templates" guard keeps the legacy deployed copy
    # (team-management/statusline.py) on its own CLAUDE_PROJECT_DIR branch below.
    _PLUGIN_IMPORT = True
    sys.path.insert(0, str(_self_hooks))
    from shared_state import edit_state, Model, Mode, find_git_repo, load_state, IconStyle, PROJECT_ROOT, load_config, get_model_context_limit, get_context_length_from_transcript, read_last_jsonl_entry, check_workflow_bypass, get_protocol_state, load_protocol_config
elif 'CLAUDE_PROJECT_DIR' in os.environ:
    PROJECT_ROOT = Path(os.environ['CLAUDE_PROJECT_DIR']).resolve()
    # Add .claude directory to path for importing hooks
    sys.path.insert(0, str(PROJECT_ROOT / ".claude"))
    # Import from .claude/hooks directory
    from hooks.shared_state import edit_state, Model, Mode, find_git_repo, load_state, IconStyle, load_config, get_model_context_limit, get_context_length_from_transcript, read_last_jsonl_entry, check_workflow_bypass, get_protocol_state, load_protocol_config
else:
    # No import branch matched: not a plugin process (no CLAUDE_PLUGIN_ROOT), not
    # the plugin's own templates/ copy (self-locate above), and no
    # CLAUDE_PROJECT_DIR. The pip-era `plugin.hooks` install path was retired
    # (#7), so there is nothing left to import from — fail loudly here instead of
    # a NameError deep inside rendering.
    raise ImportError(
        "statusline: cannot locate shared_state — expected CLAUDE_PLUGIN_ROOT, a "
        "templates/ sibling hooks dir, or CLAUDE_PROJECT_DIR in the environment."
    )
##-##

#-#

# ===== FUNCTIONS ===== #

def find_current_transcript(transcript_path, session_id, stale_threshold=30):
    """
    Detect stale transcripts and find the current one by session ID.

    Args:
        transcript_path: Path to the transcript file we received
        session_id: Current session ID to match
        stale_threshold: Seconds threshold for considering transcript stale

    Returns:
        Path to the current transcript (may be same as input if not stale)
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return transcript_path

    try:
        # Read only the LAST JSONL entry via a bounded tail-read (seek from end),
        # never the whole session file — it grows to tens of MB in long sessions
        # and this runs on every prompt.
        last_msg = read_last_jsonl_entry(transcript_path)
        if not last_msg: return transcript_path

        last_timestamp = last_msg.get('timestamp')
        if not isinstance(last_timestamp, str): return transcript_path

        # Parse ISO timestamp and compare to current time
        last_time = datetime.fromisoformat(last_timestamp.replace('Z', '+00:00'))
        current_time = datetime.now(timezone.utc)
        age_seconds = (current_time - last_time).total_seconds()

        # If transcript is fresh, return it
        if age_seconds <= stale_threshold: return transcript_path

        # Transcript is stale - search for current one
        transcript_dir = Path(transcript_path).parent
        all_transcripts = sorted(
            transcript_dir.glob('*.jsonl'),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )[:5]  # Top 5 most recent

        # Check each transcript for matching session ID (tail-read, not readlines)
        for candidate in all_transcripts:
            try:
                candidate_last = read_last_jsonl_entry(str(candidate))
                if not candidate_last: continue

                if candidate_last.get('sessionId') == session_id:
                    # Verify this transcript is fresh
                    candidate_timestamp = candidate_last.get('timestamp')
                    if isinstance(candidate_timestamp, str):
                        candidate_time = datetime.fromisoformat(candidate_timestamp.replace('Z', '+00:00'))
                        candidate_age = (current_time - candidate_time).total_seconds()

                        if candidate_age <= stale_threshold: return str(candidate)
            except (OSError, ValueError): continue

        # No fresh transcript found, return original
        return transcript_path

    except (OSError, ValueError): return transcript_path # Any error, return original path

#-#

# ===== GLOBALS ===== #

#!> Parse input + set constants
# read json input from stdin
try:
    data = json.load(sys.stdin)
except Exception as e:
    # Fatal error - can't even parse input
    print(f"⚠️  Statusline Fatal Error: Failed to parse input")
    print(f"📍 Error: {e}")
    sys.exit(1)

# Wrap everything in try/except for error visibility
try:
    cwd = data.get("cwd", ".")
    model_name = data.get("model", {}).get("display_name", "unknown")
    model_id = data.get("model", {}).get("id", "")
    session_id = data.get("session_id", "unknown")

    task_dir = PROJECT_ROOT / "team-management" / "tasks"
    #!<

    #!> Colors/styles - with Windows ANSI detection
    def supports_ansi():
        """Check if the current environment supports ANSI color codes."""
        # Windows detection
        if sys.platform == 'win32':
            # Windows Terminal and PowerShell 7+ support ANSI
            wt_session = os.environ.get('WT_SESSION')
            pwsh_version = os.environ.get('POWERSHELL_DISTRIBUTION_CHANNEL')

            # Windows Terminal always supports ANSI
            if wt_session:
                return True

            # PowerShell 7+ supports ANSI
            if pwsh_version and 'PSCore' in pwsh_version:
                return True

            # Try to enable ANSI on Windows 10+
            try:
                import platform
                win_ver = platform.version()
                # Windows 10 build 14393+ supports ANSI with VT100 mode
                if int(win_ver.split('.')[2]) >= 14393:
                    # Enable VT100 processing
                    import ctypes
                    kernel32 = ctypes.windll.kernel32
                    # Get stdout handle
                    stdout_handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
                    # Get current mode
                    mode = ctypes.c_ulong()
                    kernel32.GetConsoleMode(stdout_handle, ctypes.byref(mode))
                    # Enable ENABLE_VIRTUAL_TERMINAL_PROCESSING (0x0004)
                    mode.value |= 0x0004
                    kernel32.SetConsoleMode(stdout_handle, mode.value)
                    return True
            except Exception:
                # Best-effort ctypes/VT100 enable — any failure falls through to
                # "no ANSI"; never crash the statusline over color support.
                pass

            # Fallback: no ANSI support on old Windows
            return False

        # Unix-like systems support ANSI
        return True

    # Determine if ANSI is supported
    ansi_supported = supports_ansi()

    # Define colors based on ANSI support
    if ansi_supported:
        green = "\033[38;5;114m"
        orange = "\033[38;5;215m"
        red = "\033[38;5;203m"
        gray = "\033[38;5;242m"
        l_gray = "\033[38;5;250m"
        cyan = "\033[38;5;111m"
        purple = "\033[38;5;183m"
        reset = "\033[0m"
    else:
        # No color support - use empty strings
        green = orange = red = gray = l_gray = cyan = purple = reset = ""
    #!<

    #!> Determine model and context limit (using shared utilities)
    curr_model = None
    # Get model context limit using shared utility
    context_limit = get_model_context_limit(model_name, model_id)

    if "sonnet" in model_name.lower(): curr_model = Model.SONNET
    elif "opus" in model_name.lower(): curr_model = Model.OPUS
    else: curr_model = Model.UNKNOWN
    #!<

    #!> Update model in shared state
    STATE = load_state()
    if not STATE or STATE.model != curr_model:
        with edit_state() as s: s.model = curr_model; STATE = s

    # Load config for icon style preference (already imported at top)
    CONFIG = load_config()
    # Use icon_style from config - load_config() defaults to ASCII which is safest
    icon_style = CONFIG.features.icon_style if CONFIG else IconStyle.ASCII
    #!<

    #-#

    """
    ╔═══════════════════════════════════════════════════════════════════════════╗
    ║ ██████╗██████╗ █████╗ ██████╗██╗ ██╗██████╗██╗     ██████╗██╗  ██╗██████╗ ║
    ║ ██╔═══╝╚═██╔═╝██╔══██╗╚═██╔═╝██║ ██║██╔═══╝██║     ╚═██╔═╝███╗ ██║██╔═══╝ ║
    ║ ██████╗  ██║  ███████║  ██║  ██║ ██║██████╗██║       ██║  ████╗██║█████╗  ║
    ║ ╚═══██║  ██║  ██╔══██║  ██║  ██║ ██║╚═══██║██║       ██║  ██╔████║██╔══╝  ║
    ║ ██████║  ██║  ██║  ██║  ██║  ╚████╔╝██████║███████╗██████╗██║╚███║██████╗ ║
    ║ ╚═════╝  ╚═╝  ╚═╝  ╚═╝  ╚═╝   ╚═══╝ ╚═════╝╚══════╝╚═════╝╚═╝ ╚══╝╚═════╝ ║
    ╚═══════════════════════════════════════════════════════════════════════════╝
    Sessions default status line script
    Shows:
    - Context usage progress bar (with Ayu Dark colors)
    - Current task name
    - Current mode (Discussion or Implementation)
    - Count of edited & uncommitted files in the current git repo
    - Count of open tasks in team-management/tasks (files + dirs)
    """

    # ===== EXECUTION ===== #

    ## ===== PROGRESS BAR ===== ##

    #!> Pull context length from transcript (using shared utility)
    context_length = None
    transcript_path = data.get('transcript_path', None)

    # Detect and recover from stale transcript
    if transcript_path:
        transcript_path = find_current_transcript(transcript_path, session_id)

    if transcript_path:
        # Use shared utility to get context length
        context_length = get_context_length_from_transcript(transcript_path)
    #!<

    #!> Use context_length and context_limit to calculate context percentage
    if context_length and context_length < 17000: context_length = 17000
    if context_length and context_limit:
        pct = (context_length * 100) / context_limit
        progress_pct = f"{pct:.1f}"
        progress_pct_int = int(pct)
        if progress_pct_int > 100: progress_pct = "100.0"; progress_pct_int = 100
    else:
        progress_pct = "0.0"
        progress_pct_int = 0
    #!<

    #!> Formatting and styling
    # Format token counts in 'k'
    formatted_tokens = f"{context_length // 1000}k" if context_length else "17k"
    formatted_limit = f"{context_limit // 1000}k" if context_limit else "160k"

    # Progress bar blocks (0-10)
    filled_blocks = min(progress_pct_int // 10, 10)
    empty_blocks = 10 - filled_blocks

    # Ayu Dark colors (referencing from memory)
    # TODO: Verify Ayu Dark code conversions
    if progress_pct_int < 50: bar_color =  green
    elif progress_pct_int < 80: bar_color = orange
    else: bar_color = red
    #!<

    #!> Construct progress bar string
    # Extract model display name (e.g., "Claude Sonnet 4.5 [1m]" -> "Sonnet 4.5")
    model_display = model_name
    if "Claude " in model_name:
        model_display = model_name.replace("Claude ", "")
    # Remove [1m] suffix if present
    if "[1m]" in model_display:
        model_display = model_display.replace(" [1m]", "").replace("[1m]", "")
    # Remove [200k] suffix if present
    if "[200k]" in model_display:
        model_display = model_display.replace(" [200k]", "").replace("[200k]", "")
    # Keep (1M context) and (200k context) text - don't remove it

    # Build progress bar string with model name
    progress_bar = []
    progress_bar.append(f"{cyan}{model_display}:{reset} ")
    if icon_style == IconStyle.NERD_FONTS:
        context_icon = "󱃖 "
    elif icon_style == IconStyle.EMOJI:
        context_icon = ""
    else:  # ASCII
        context_icon = ""
    progress_bar.append(f"{l_gray}{context_icon}")
    progress_bar.append(bar_color + ("█" * filled_blocks))
    progress_bar.append(gray + ("░" * empty_blocks))
    progress_bar.append(reset + f" {l_gray}{progress_pct}% ({formatted_tokens}/{formatted_limit}){reset}")

    progress_bar_str = "".join(progress_bar)
    #!<
    ##-##

    ## ===== GIT REPOSITORY ===== ##
    # Find git repository path for use in multiple sections
    git_path = find_git_repo(Path(cwd))
    ##-##

    ## ===== GIT BRANCH & UPSTREAM TRACKING ===== ##
    git_branch_info = None
    upstream_info = None
    if git_path:
        try:
            # Get current branch
            # Use absolute paths to avoid Windows path issues
            cwd_abs = str(Path(cwd).resolve())
            branch_cmd = ["git", "-C", cwd_abs, "branch", "--show-current"]
            branch = subprocess.check_output(branch_cmd, stderr=subprocess.PIPE, encoding='utf-8', errors='replace', timeout=5).strip()

            if branch:
                if icon_style == IconStyle.NERD_FONTS:
                    branch_icon = "󰘬 "
                elif icon_style == IconStyle.EMOJI:
                    branch_icon = "Branch: "
                else:  # ASCII
                    branch_icon = "Branch: "
                git_branch_info = f"{l_gray}{branch_icon}{branch}{reset}"

                # Get upstream tracking status
                try:
                    ahead_cmd = ["git", "-C", cwd_abs, "rev-list", "--count", "@{u}..HEAD"]
                    ahead = int(subprocess.check_output(ahead_cmd, stderr=subprocess.PIPE, encoding='utf-8', errors='replace', timeout=5).strip())

                    behind_cmd = ["git", "-C", cwd_abs, "rev-list", "--count", "HEAD..@{u}"]
                    behind = int(subprocess.check_output(behind_cmd, stderr=subprocess.PIPE, encoding='utf-8', errors='replace', timeout=5).strip())

                    upstream_parts = []
                    if ahead > 0:
                        upstream_parts.append(f"↑ {ahead}")
                    if behind > 0:
                        upstream_parts.append(f"↓ {behind}")
                    if upstream_parts:
                        upstream_info = f"{orange}{''.join(upstream_parts)}{reset}"
                except (subprocess.SubprocessError, OSError, ValueError):
                    # No upstream or error getting upstream status
                    upstream_info = None
            else:
                # Detached HEAD - show commit hash with detached indicator
                commit_cmd = ["git", "-C", cwd_abs, "rev-parse", "--short", "HEAD"]
                commit = subprocess.check_output(commit_cmd, stderr=subprocess.PIPE, encoding='utf-8', errors='replace', timeout=5).strip()
                if commit:
                    if icon_style == IconStyle.NERD_FONTS:
                        # Broken link icon to indicate detached
                        git_branch_info = f"{l_gray}󰌺 @{commit}{reset}"
                    else:  # EMOJI or ASCII
                        git_branch_info = f"{l_gray}@{commit} [detached]{reset}"
        except (subprocess.CalledProcessError, OSError, ValueError) as e:
            # Git command failed - common on Windows if git not in PATH or repo issues
            git_branch_info = None
    ##-##

    ## ===== MCP SERVER STATUS ===== ##
    def get_mcp_servers():
        """Get MCP server status by reading config files directly.

        Reads from both .mcp.json (project root) and .claude/settings.json
        to get the full list of configured MCP servers. This approach avoids
        subprocess calls which timeout on Windows due to shell=True issues.
        """
        try:
            mcp_servers = {}

            # The plugin's own MCP server (key 'tm', namespaced team-management) is
            # registered via the plugin manifest — NOT the project config files
            # read below — so it never appears there. Surface it explicitly under
            # its full name when running as a plugin. Using it as a dict key
            # dedupes against a project entry that happens to share the name.
            if _PLUGIN_IMPORT:
                mcp_servers["team-management"] = {}

            # Read from .mcp.json at project root
            mcp_json_file = PROJECT_ROOT / ".mcp.json"
            if mcp_json_file.exists():
                with open(mcp_json_file, 'r', encoding='utf-8') as f:
                    mcp_config = json.load(f)
                    mcp_servers.update(mcp_config.get("mcpServers", {}))

            # Read from .claude/settings.json
            settings_file = PROJECT_ROOT / ".claude" / "settings.json"
            if settings_file.exists():
                with open(settings_file, 'r', encoding='utf-8') as f:
                    settings = json.load(f)
                    mcp_servers.update(settings.get("mcpServers", {}))

            if not mcp_servers:
                return None

            # List configured server names. We deliberately do NOT append a
            # "(✓) connected" marker: this function only reads config files and
            # cannot verify a live connection, so a checkmark would be fiction.
            servers = list(sorted(mcp_servers.keys()))
            return " ".join(servers) if servers else None
        except Exception:
            return None

    mcp_servers = get_mcp_servers()
    ##-##

    ## ===== CURRENT TASK ===== ##
    curr_task = STATE.current_task.name if STATE else None
    ##-##

    ## ===== PROTOCOL STATE ===== ##
    protocol_info = None
    try:
        protocol_state = get_protocol_state()
        if protocol_state and protocol_state.get("name"):
            proto_name = protocol_state["name"]
            proto_step = protocol_state.get("current_step", 0)
            proto_step_name = protocol_state.get("step_name", "")
            # Get total steps from protocol config
            proto_config = load_protocol_config(proto_name)
            proto_total = len(proto_config.get("steps", [])) if proto_config else "?"
            protocol_info = f"{purple}{proto_name} {proto_step + 1}/{proto_total} {proto_step_name}{reset}"
    except Exception:
        pass

    if icon_style == IconStyle.NERD_FONTS:
        proto_icon = "󰣪 "
    elif icon_style == IconStyle.EMOJI:
        proto_icon = "Protocol: "
    else:  # ASCII
        proto_icon = "Protocol: "
    if not protocol_info:
        protocol_info = f"{cyan}{proto_icon}{gray}---{reset}"
    else:
        protocol_info = f"{cyan}{proto_icon}{reset}{protocol_info}"
    ##-##

    ## ===== CURRENT MODE ===== ##
    # Check if workflow is fully disabled (no hooks in settings.json). This
    # inference is only valid in legacy/dev mode, where hooks are registered in
    # the project .claude/settings.json. In plugin mode hooks live in the plugin's
    # hooks.json, so their absence from project settings is NOT a disable — skip
    # the inference entirely (m-fix-plugin-mode-install-bugs).
    hooks_disabled = False
    if not _PLUGIN_IMPORT:
        try:
            settings_file = PROJECT_ROOT / ".claude" / "settings.json"
            if settings_file.exists():
                with open(settings_file, 'r', encoding='utf-8') as f:
                    settings = json.load(f)
                    hooks = settings.get("hooks", {})
                    # Check if hooks section is empty or has no enforcement hooks
                    hooks_disabled = not bool(
                        hooks.get("PreToolUse") or
                        hooks.get("PostToolUse") or
                        hooks.get("UserPromptSubmit") or
                        hooks.get("SessionStart")
                    )
        except (OSError, ValueError, AttributeError):
            # AttributeError covers a valid-but-non-dict settings.json root (or a
            # non-dict "hooks") whose .get() would raise — degrade in place
            # (hooks_disabled stays False) rather than surfacing a statusline error.
            pass

    # Check if workflow bypass is active
    workflow_bypassed = check_workflow_bypass()

    if hooks_disabled:
        # Full disable mode - hooks completely removed
        curr_mode = "DISABLED"
        if icon_style == IconStyle.NERD_FONTS:
            mode_icon = "󰿘 "  # Disabled/off icon
        elif icon_style == IconStyle.EMOJI:
            mode_icon = "🚫 "
        else:  # ASCII
            mode_icon = "Mode:"
        # Use red for disabled
        mode_color = red
    elif workflow_bypassed:
        # Bypass mode - hooks active but bypassing enforcement
        curr_mode = "BYPASS"
        if icon_style == IconStyle.NERD_FONTS:
            mode_icon = "󰜺 "  # Warning/bypass icon
        elif icon_style == IconStyle.EMOJI:
            mode_icon = "⚠️ "
        else:  # ASCII
            mode_icon = "Mode:"
        # Use orange/warning color for bypass
        mode_color = orange
    else:
        # Normal mode display - check for documentation mode via state file
        daic_mode_str = "discussion"
        try:
            daic_state_file = PROJECT_ROOT / ".claude" / "state" / "daic-mode.json"
            if daic_state_file.exists():
                with open(daic_state_file, 'r', encoding='utf-8') as f:
                    daic_mode_str = json.load(f).get("mode", "discussion")
        except Exception:
            pass

        if daic_mode_str == "implementation":
            curr_mode = "Implement"
            if icon_style == IconStyle.NERD_FONTS:
                mode_icon = "󰷫 "
            elif icon_style == IconStyle.EMOJI:
                mode_icon = "🛠️: "
            else:
                mode_icon = "Mode:"
            mode_color = purple
        elif daic_mode_str == "documentation":
            curr_mode = "Document"
            if icon_style == IconStyle.NERD_FONTS:
                mode_icon = "󰈙 "
            elif icon_style == IconStyle.EMOJI:
                mode_icon = "📝: "
            else:
                mode_icon = "Mode:"
            mode_color = cyan
        else:
            curr_mode = "Discuss"
            if icon_style == IconStyle.NERD_FONTS:
                mode_icon = "󰭹 "
            elif icon_style == IconStyle.EMOJI:
                mode_icon = "💬:"
            else:
                mode_icon = "Mode:"
            mode_color = purple
    ##-##

    ## ===== COUNT EDITED & UNCOMMITTED ===== ##
    # Use subprocess to count edited and uncommitted files (unstaged or staged)
    total_edited = 0
    if git_path:
        try:
            # Use absolute paths for Windows compatibility
            cwd_abs = str(Path(cwd).resolve())

            # Count unstaged changes
            unstaged_cmd = ["git", "-C", cwd_abs, "diff", "--name-only"]
            unstaged_files = subprocess.check_output(unstaged_cmd, stderr=subprocess.PIPE, encoding='utf-8', errors='replace', timeout=5).strip().split('\n')
            unstaged_count = len([f for f in unstaged_files if f])  # Filter out empty strings

            # Count staged changes
            staged_cmd = ["git", "-C", cwd_abs, "diff", "--cached", "--name-only"]
            staged_files = subprocess.check_output(staged_cmd, stderr=subprocess.PIPE, encoding='utf-8', errors='replace', timeout=5).strip().split('\n')
            staged_count = len([f for f in staged_files if f])  # Filter out empty strings

            total_edited = unstaged_count + staged_count
        except (subprocess.CalledProcessError, OSError, ValueError):
            # Git command failed - set to 0 and continue
            total_edited = 0
    ##-##

    ## ===== COUNT OPEN TASKS ===== ##
    open_task_count = 0
    open_task_dir_count = 0

    if task_dir.exists() and task_dir.is_dir():
        for file in task_dir.iterdir():
            if file.is_file() and file.name != "TEMPLATE.md" and file.suffix == ".md": open_task_count += 1
            if file.is_dir() and file.name not in ("done", "indexes"): open_task_dir_count += 1
    ##-##

    ## ===== FINAL OUTPUT ===== ##
    # Line 1 - Progress bar | Task | Branch (MOVED HERE)
    context_part = progress_bar_str if progress_bar_str else f"{gray}No context usage data{reset}"
    if icon_style == IconStyle.NERD_FONTS:
        task_icon = "󰒓 "
    elif icon_style == IconStyle.EMOJI:
        task_icon = "⚙️ "
    else:  # ASCII
        task_icon = "Task: "
    task_part = f"{cyan}{task_icon}{curr_task}{reset}" if curr_task else f"{cyan}{task_icon}{gray}No Task{reset}"

    # Add protocol and branch to line 1
    line1_parts = [context_part, task_part]
    line1_parts.append(protocol_info)
    if git_branch_info:
        line1_parts.append(git_branch_info)
    print(" | ".join(line1_parts))

    # Line 2 - Mode | Edited & Uncommitted with upstream | Open Tasks | MCP (NEW)
    if icon_style == IconStyle.NERD_FONTS:
        tasks_icon = "󰈙 "
    elif icon_style == IconStyle.EMOJI:
        tasks_icon = "💼 "
    else:  # ASCII
        tasks_icon = ""
    # Build uncommitted section with optional upstream indicators
    uncommitted_parts = [f"{orange}✎ {total_edited}{reset}"]
    if upstream_info:
        uncommitted_parts.append(upstream_info)
    uncommitted_str = " ".join(uncommitted_parts)

    line2_parts = [
        f"{mode_color}{mode_icon} {curr_mode}{reset}",
        uncommitted_str,
        f"{cyan}{tasks_icon} {open_task_count + open_task_dir_count} open{reset}"
    ]

    # Add project name to line 2 (between open tasks and MCP). Configured
    # `project_name` wins; empty/unset falls back to the project folder name.
    project_name = (getattr(CONFIG, 'project_name', None) or PROJECT_ROOT.name)
    if icon_style == IconStyle.NERD_FONTS:
        proj_icon = "󰉋 "
    elif icon_style == IconStyle.EMOJI:
        proj_icon = "📁 "
    else:  # ASCII - label style (no leading space) like the MCP segment
        proj_icon = "Project: "
    line2_parts.append(f"{l_gray}{proj_icon}{project_name}{reset}")

    # Add MCP servers to line 2 (NEW)
    if mcp_servers:
        if icon_style == IconStyle.NERD_FONTS:
            mcp_icon = "󰒍 "
        elif icon_style == IconStyle.EMOJI:
            mcp_icon = "🔌 "
        else:
            mcp_icon = "MCP: "
        line2_parts.append(f"{l_gray}{mcp_icon}{mcp_servers}{reset}")

    print(" | ".join(line2_parts))
    ##-##

    #-#

# End of try block - catch any errors and display them
except Exception as e:
    import traceback
    print(f"⚠️  Statusline Error: {type(e).__name__}: {e}")
    print(f"📍 Set STATUSLINE_DEBUG=1 for a traceback")
    if os.environ.get('STATUSLINE_DEBUG'):
        print(f"🐛 Traceback:\n{traceback.format_exc()}")
    sys.exit(1)