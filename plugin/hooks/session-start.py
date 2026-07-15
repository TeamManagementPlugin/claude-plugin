#!/usr/bin/env python3
"""Session start hook to initialize team-management context."""
import json
import sys
from shared_state import (
    get_project_root, get_plugin_root, is_plugin_mode, ensure_state_dir,
    get_task_state, check_workflow_bypass,
    get_workflow_bypass_state, set_daic_mode, ensure_discussion_mode_best_effort,
    load_protocol_config,
    resolve_protocol_start_text, reset_subagent_depth,
    parse_task_frontmatter, _write_json_durable, ensure_task_template_deployed,
    ensure_provider_tokens_file, ensure_claude_dir_gitignored,
    ensure_guidance_deployed_and_wired
)
from boot_detector import detect_legacy_install

# Get project root
PROJECT_ROOT = get_project_root()

# Read the SessionStart hook payload once. `source` is one of
# startup / resume / clear / compact. Guarded so empty/malformed stdin never
# breaks session start (mirrors pre-compact.py's defensive read).
try:
    _hook_input = json.load(sys.stdin)
    if not isinstance(_hook_input, dict):
        _hook_input = {}
except (json.JSONDecodeError, ValueError):
    _hook_input = {}
session_source = _hook_input.get("source", "")

# Get developer name from config
try:
    CONFIG_FILE = PROJECT_ROOT / 'team-management' / 'config.json'
    config_missing = False
    
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
            developer_name = config.get('developer_name', 'the developer')
    else:
        developer_name = 'the developer'
        config_missing = True
except (FileNotFoundError, json.JSONDecodeError, OSError):
    developer_name = 'the developer'
    config_missing = True

# Initialize context
context = f"""You are beginning a new context window with {developer_name}.

"""

# Quick configuration checks
needs_setup = False
quick_checks = []

# Check if tiktoken is installed (required for subagent transcript chunking)
try:
    import tiktoken  # noqa: F401 — availability probe; ImportError → install hint below
except ImportError:
    needs_setup = True
    quick_checks.append("tiktoken (pip install tiktoken)")

# Ensure state directory exists (DAIC mode set after protocol check below)
ensure_state_dir()

# (MCP-cache block removed — native plugin MCP registration makes the
# `claude mcp list` shell-out obsolete; statusline MCP status is rewired in #5.)

# DAIC mode will be set after protocol check:
# - If protocol active: set from protocol step's mode
# - If no protocol: default to discussion

# Check if workflow bypass is active (persists across sessions)
if check_workflow_bypass():
    bypass_state = get_workflow_bypass_state()
    context += """[WORKFLOW BYPASS ACTIVE]
All DAIC enforcement is bypassed. Hooks run but skip enforcement.
To re-enable: run  python3 $CLAUDE_PLUGIN_ROOT/hooks/workflow_command.py bypass disable

"""
    if bypass_state.get("reason"):
        context += f"Bypass reason: {bypass_state['reason']}\n\n"

# Clear context warning flags for new session. Route every unlink through
# _safe_unlink so a PermissionError (Windows file lock / read-only fs) can't
# kill SessionStart.
def _safe_unlink(p):
    """Best-effort unlink — never let an undeletable flag kill SessionStart."""
    try:
        p.unlink()
    except OSError:
        pass

# First, clear global flags (legacy support)
warning_80_flag = PROJECT_ROOT / '.claude' / 'state' / 'context-warning-80.flag'
warning_90_flag = PROJECT_ROOT / '.claude' / 'state' / 'context-warning-90.flag'
if warning_80_flag.exists():
    _safe_unlink(warning_80_flag)
if warning_90_flag.exists():
    _safe_unlink(warning_90_flag)

# Clear auto-compact flags for fresh session
auto_compact_flag = PROJECT_ROOT / '.claude' / 'state' / 'auto-compact-triggered.flag'
if auto_compact_flag.exists():
    _safe_unlink(auto_compact_flag)
# compact-pending.flag carries the post-compact restoration checkpoint that
# user-messages.py consumes on the next UserPromptSubmit. A `resume` continues
# the same logical session, so clearing it here would erase a not-yet-restored
# checkpoint — only clear it on a genuinely fresh start (startup/clear/compact).
compact_pending_flag = PROJECT_ROOT / '.claude' / 'state' / 'compact-pending.flag'
if compact_pending_flag.exists() and session_source != "resume":
    _safe_unlink(compact_pending_flag)

# Reset the subagent-context depth counter for a fresh session — a session that
# crashed mid-subagent would otherwise leave a non-zero depth that makes the
# main agent silently bypass DAIC enforcement. Also remove the legacy boolean
# flag from pre-counter installs so it can never be misread.
try:
    reset_subagent_depth()
except Exception:
    pass
legacy_subagent_flag = PROJECT_ROOT / '.claude' / 'state' / 'in_subagent_context.flag'
if legacy_subagent_flag.exists():
    _safe_unlink(legacy_subagent_flag)

# Clear AI-providers migration warning flag so it re-emits once per session if
# legacy keys are still present in config.json. Mirrors compact-flag pattern.
ai_providers_warn_flag = PROJECT_ROOT / '.claude' / 'state' / 'ai-providers-migration-warned.flag'
if ai_providers_warn_flag.exists():
    _safe_unlink(ai_providers_warn_flag)

# Emit one-time deprecation warning for legacy AI-provider keys.
# Legacy keys (`include_in_architecture`, `include_in_exploration`,
# `gemini.default_model`) are read here ONLY to surface the warning; their
# values are intentionally NOT auto-forwarded to the new per-phase keys
# (silent semantic expansion was rejected in brainstorm Round 2 — a user
# with `include_in_architecture: true` under the old code-review-only meaning
# would otherwise silently get providers in 3 additional phases on upgrade).
try:
    if not config_missing:
        ai_legacy = config.get('ai_providers', {}) if isinstance(config, dict) else {}
        gemini_legacy = config.get('gemini', {}) if isinstance(config, dict) else {}
        legacy_keys_present = []
        if 'include_in_architecture' in ai_legacy:
            legacy_keys_present.append('ai_providers.include_in_architecture')
        if 'include_in_exploration' in ai_legacy:
            legacy_keys_present.append('ai_providers.include_in_exploration')
        if 'default_model' in gemini_legacy:
            legacy_keys_present.append('gemini.default_model')
        if legacy_keys_present:
            context += (
                "\n[AI providers — deprecation notice] Your team-management/config.json "
                "still contains legacy key(s): " + ", ".join(legacy_keys_present) + ". "
                "These are no longer read by the engine. The replacements are 6 explicit "
                "per-phase flags under `ai_providers.*` "
                "(include_in_code_review, include_in_brainstorm, include_in_investigation, "
                "include_in_implementation, include_in_research_exploration, "
                "include_in_refactoring_planning). Legacy values are NOT auto-forwarded — "
                "use /team-management:config (or hand-edit config.json) to enable the phases you want. "
                "Remove the legacy keys when done. This notice fires once per session.\n"
            )
            ai_providers_warn_flag.parent.mkdir(parents=True, exist_ok=True)
            ai_providers_warn_flag.write_text('warned\n', encoding='utf-8')
except (AttributeError, TypeError, OSError):
    # Best-effort warning — never block session start on a malformed config.
    pass

# Emit one-time replacement warning for the retired gemini provider.
# The gemini provider was replaced by agy (Google Antigravity CLI); a stale
# `gemini.enabled: true` or a `"gemini"` entry in `enabled_providers` is dead
# config — the engine only dispatches codex / agy. Values are intentionally
# NOT auto-forwarded to `agy.*` (same no-auto-forward policy as the legacy
# per-phase keys above). Uses its OWN flag file: the migration flag above is
# already present on pre-replacement installs and would suppress this notice.
gemini_replaced_flag = PROJECT_ROOT / '.claude' / 'state' / 'ai-providers-gemini-replaced-warned.flag'
if gemini_replaced_flag.exists():
    _safe_unlink(gemini_replaced_flag)
try:
    if not config_missing:
        ai_block = config.get('ai_providers', {}) if isinstance(config, dict) else {}
        gemini_block = config.get('gemini', {}) if isinstance(config, dict) else {}
        gemini_remnants = []
        if isinstance(gemini_block, dict) and gemini_block.get('enabled') is True:
            gemini_remnants.append('gemini.enabled: true')
        enabled_list = ai_block.get('enabled_providers', []) if isinstance(ai_block, dict) else []
        if isinstance(enabled_list, list) and 'gemini' in enabled_list:
            gemini_remnants.append('"gemini" in ai_providers.enabled_providers')
        if gemini_remnants:
            context += (
                "\n[AI providers — gemini replaced by agy] Your team-management/config.json "
                "still references the retired gemini provider (" + ", ".join(gemini_remnants) + "). "
                "The gemini integration was replaced by the Google Antigravity CLI (`agy`); "
                "these entries are no longer read by the engine. Values are NOT auto-forwarded — "
                "use /team-management:config to enable `agy` explicitly, then remove the "
                "stale gemini block. Custom `gemini-*.md` provider templates under "
                "protocol-configs/custom/providers/ are no longer used (lookup is by `agy-*` name). "
                "This notice fires once per session.\n"
            )
            gemini_replaced_flag.parent.mkdir(parents=True, exist_ok=True)
            gemini_replaced_flag.write_text('warned\n', encoding='utf-8')
except (AttributeError, TypeError, OSError):
    # Best-effort warning — never block session start on a malformed config.
    pass

# Check if sessions directory exists
sessions_dir = PROJECT_ROOT / 'team-management'
if sessions_dir.exists():
    # Check for active task
    task_state = get_task_state()
    if task_state.get("task"):
        task_file = sessions_dir / 'tasks' / f"{task_state['task']}.md"
        # Directory-style tasks live at tasks/<task>/README.md, not <task>.md —
        # fall back so their context loads at session start too.
        if not task_file.exists():
            dir_task_readme = sessions_dir / 'tasks' / task_state['task'] / 'README.md'
            if dir_task_readme.exists():
                task_file = dir_task_readme
        if task_file.exists():
            # Read task content (read-only — status updates via MCP tools)
            task_content = task_file.read_text(encoding='utf-8')

            # Check status via the shared frontmatter parser (read-only)
            task_has_pending_status = (
                parse_task_frontmatter(task_state['task']).get('status') == 'pending'
            )
            
            # Output the full task state
            context += f"""Current task state:
```json
{json.dumps(task_state, indent=2)}
```

Loading task file: {task_state['task']}.md
{"=" * 60}
{task_content}
{"=" * 60}
"""
            
            if task_has_pending_status:
                context += """
[Note: Task status is 'pending'. Start a protocol or use MCP tools to update status.]
"""
            else:
                context += """
Review the Work Log at the end of the task file above.
Continue from where you left off, updating the work log as you progress.
"""
    else:
        # No active task - list available tasks
        tasks_dir = sessions_dir / 'tasks'
        task_files = []
        if tasks_dir.exists():
            task_files = sorted([f for f in tasks_dir.glob('*.md') if f.name != 'TEMPLATE.md'])
        
        if task_files:
            context += """No active task set. Available tasks:

"""
            for task_file in task_files:
                task_name = task_file.stem
                status = parse_task_frontmatter(task_name).get('status', 'unknown')
                context += f"  • {task_name} ({status})\n"
            
            context += """
To work on a task, start a protocol:
  mcp__plugin_team-management_tm__protocol_start(protocol_name="task")
The protocol will set up task state, branch, and DAIC mode automatically.
"""
        else:
            context += """No tasks found.

To create your first task, start a protocol:
  mcp__plugin_team-management_tm__protocol_start(protocol_name="task")
The protocol will guide task creation, branch setup, and workflow.
"""
else:
    # team-management directory doesn't exist - likely first run
    context += """team-management is not yet initialized.

Install the team-management plugin, then run /team-management:init to enable it
for this project. See docs/INSTALL.md for marketplace and dev-mode setup.
"""

# ==================================================================
# PROTOCOL STATE INJECTION (independent of task existence)
# ==================================================================
# If a protocol is active, inject the current step's start and end.
# This works even when task=null (e.g., investigation step before task creation).
# If no protocol active, suggest starting one (if enabled).
protocol_engine_enabled = True
try:
    tm_config_file = PROJECT_ROOT / "team-management" / "config.json"
    if tm_config_file.exists():
        with open(tm_config_file, 'r', encoding='utf-8') as f:
            tm_config = json.load(f)
        protocol_engine_enabled = tm_config.get("protocol_engine", {}).get("enabled", True)
except Exception:
    pass

daic_mode_set_by_protocol = False

if protocol_engine_enabled:
    try:
        _ps_task_state = get_task_state()
        protocol_info = _ps_task_state.get("protocol")

        if protocol_info:
            protocol_name = protocol_info.get("name")
            step_name = protocol_info.get("step_name")
            current_step_idx = protocol_info.get("current_step", 0)

            protocol_config = load_protocol_config(protocol_name)

            if protocol_config:
                steps = protocol_config.get("steps", [])
                if current_step_idx < len(steps):
                    step = steps[current_step_idx]
                    start_text = step.get("start", "")
                    end_text = step.get("end", "")
                    mode = step.get("mode", "discussion")
                    total = len(steps)

                    # Restore DAIC mode from protocol step
                    set_daic_mode(mode)
                    daic_mode_set_by_protocol = True

                    # Resolve @-references
                    start_text = resolve_protocol_start_text(start_text, protocol_name)

                    context += f"""
[Protocol: "{protocol_name}" - Step {current_step_idx + 1}/{total}: "{step_name}" (mode: {mode})]

INSTRUCTIONS FOR THIS STEP:
{start_text}

COMPLETION CONDITION (call protocol_advance when this is true):
{end_text}

To advance: mcp__plugin_team-management_tm__protocol_advance(summary="<what you accomplished>")
To check status: mcp__plugin_team-management_tm__protocol_current()
"""
                    # Resume marker
                    context += f"""
[RESUME POINT] You are resuming a protocol session.
- Protocol: {protocol_name}
- Current step: {current_step_idx + 1}/{total} - "{step_name}"
- Mode: {mode}
- Task: {_ps_task_state.get('task', 'not yet created')}
- Branch: {_ps_task_state.get('branch', 'not yet created')}

Review the step instructions above and continue from where you left off.
If you need to understand what was done previously, call mcp__plugin_team-management_tm__protocol_log().
"""
        elif sessions_dir.exists():
            # No protocol active - enforce protocol-first workflow
            context += """
[PROTOCOL-FIRST WORKFLOW] All implementation work MUST go through a workflow protocol.

MANDATORY PROCESS:
1. Discuss the user's request — ask clarifying questions, propose an approach
2. Before starting ANY protocol, EXPLAIN to the user:
   - WHICH protocol you chose (e.g., "task", "research", "refactoring")
   - WHY this protocol fits their request
   - What the protocol will do (brief summary)
3. Ask for explicit permission before proceeding
4. Only after approval: start the protocol with mcp__plugin_team-management_tm__protocol_start(protocol_name="...")
5. The protocol manages DAIC mode, task files, git branches, and completion automatically
6. Do NOT call daic_mode_switch tools directly or write code outside of a protocol

CRITICAL: Never skip the explanation step. Do NOT immediately call protocol_start — always explain your reasoning and get user approval first.

Available protocols: call mcp__plugin_team-management_tm__protocol_list() to discover all available protocols.
"""
    except Exception:
        pass  # Don't fail session start on protocol injection errors

# Set DAIC mode to discussion if protocol didn't set it. Uses the fail-safe
# best-effort helper (m-fix-posttooluse-lock-failure-resilience): this no-protocol
# fallback runs on EVERY no-protocol session start, so a _state_lock acquisition
# failure on a flock-less FS (network/synced drive) must not (a) crash SessionStart
# nor (b) silently leave a stale "implementation" mode active — which would let a
# no-protocol session run with edits allowed. ensure_discussion_mode_best_effort
# degrades to an UNLOCKED write of the RESTRICTIVE discussion mode on lock failure:
# never raises, and can only tighten enforcement (never open a hole). set_daic_mode
# itself is unchanged (integrity writer, still raises).
if not daic_mode_set_by_protocol:
    ensure_discussion_mode_best_effort()

# Add config check to setup requirements
if config_missing:
    quick_checks.append("sessions config")

# If setup is needed, provide guidance  
if needs_setup or config_missing:
    context += f"""
[Setup Required]
Missing components: {', '.join(quick_checks)}

To complete setup:"""
    
    if config_missing:
        context += """
1. Run /team-management:config to create team-management/config.json (guided; no manual copy needed)
2. Set your developer name and any provider / workflow preferences when prompted"""
    
    if needs_setup:
        context += """
3. Install the team-management plugin (see docs/INSTALL.md), then run /team-management:init"""

    context += """

The sessions system helps manage tasks and maintain discussion/implementation workflow discipline.
"""

def _ensure_statusline_pinned(project_root, plugin_root):
    """Pin the plugin's statusline.py ABSOLUTE path into the project's gitignored
    .claude/settings.local.json.

    Claude Code does NOT expand ${CLAUDE_PLUGIN_ROOT} in a settings.json statusLine
    command (only inside a plugin's hooks.json), so /team-management:init cannot
    write a portable plugin-relative statusLine. This SessionStart hook DOES
    receive CLAUDE_PLUGIN_ROOT, so it resolves the absolute path here and writes it
    to the per-machine, gitignored settings.local.json (which overrides committed
    settings.json — so an existing broken install self-heals).

    Respects a user's own statusLine wherever it lives: the EFFECTIVE statusLine is
    settings.local.json's (local overrides committed) else committed settings.json's;
    if that is present and NOT one we wrote, we leave it untouched. "Ours" is anchored
    on the `templates/statusline.py` plugin-path tail (or the legacy
    ${CLAUDE_PLUGIN_ROOT} command) — NOT the bare `statusline.py` basename — so a
    user's own `*statusline.py` script is not misclassified and overwritten.

    Self-healing: a plugin update changes the version dir, so we re-pin whenever our
    stored command differs from the current resolved path. Idempotent (writes only
    when the command changes); never raises — statusline pinning must never break
    session start.

    Known trade-off: a freshly written settings.local.json is picked up on the NEXT
    session (the session that writes it may have already rendered its statusline).
    """
    try:
        statusline = (plugin_root / 'templates' / 'statusline.py').resolve()
        if not statusline.exists():
            return
        # Pin the hook's OWN interpreter (sys.executable) rather than a literal
        # `python3` token: on Windows there is no `python3.exe`, so a `python3`
        # statusLine command fails there. sys.executable is the interpreter that
        # launched this hook — guaranteed present and >=3.10 on every platform.
        desired = {'type': 'command', 'command': f'"{sys.executable}" "{statusline}"', 'padding': 0}

        def _is_ours(sl):
            # A statusLine WE wrote: the plugin template-path tail, or the legacy
            # ${CLAUDE_PLUGIN_ROOT} form. Anchored on 'templates/statusline.py' (NOT
            # the bare basename) so a user's own *statusline.py is never clobbered.
            if not isinstance(sl, dict):
                return False
            # Normalize separators: on Windows the pinned command embeds a backslash
            # path (Path.resolve()), so match both '/' and '\' spellings — otherwise a
            # Windows statusLine never self-heals across plugin-version bumps.
            cmd = sl.get('command', '').replace('\\', '/')
            return ('templates/statusline.py' in cmd) or ('CLAUDE_PLUGIN_ROOT' in cmd)

        def _read_dict(path):
            # (doc, ok): absent -> (None, True); unparseable/non-dict -> (None, False).
            if not path.exists():
                return None, True
            try:
                doc = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                return None, False
            return (doc, True) if isinstance(doc, dict) else (None, False)

        local_path = project_root / '.claude' / 'settings.local.json'
        local, ok = _read_dict(local_path)
        if not ok:
            return  # never destroy a settings.local.json we cannot parse
        local = local or {}
        local_sl = local.get('statusLine')

        # EFFECTIVE statusLine: local overrides committed. Respect a custom one in
        # EITHER file — init may leave a pre-existing user statusLine in settings.json.
        effective = local_sl
        if effective is None:
            committed, _c_ok = _read_dict(project_root / '.claude' / 'settings.json')
            if committed:
                effective = committed.get('statusLine')

        if effective is not None:
            if not _is_ours(effective):
                return  # the user's own (or non-dict) statusLine — leave it untouched
            if (isinstance(local_sl, dict)
                    and local_sl.get('command') == desired['command']):
                return  # already correct in the local file — idempotent no-op

        local['statusLine'] = desired
        local_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json_durable(local_path, local)
    except Exception:
        return  # best-effort; never break session start


# Plugin-mode guidance + boot-detector advisory. Gated on plugin-mode: in
# legacy/dev the project CLAUDE.md @-includes already carry the guidance (double
# injection would duplicate it), and the legacy hooks ARE the install (nothing to
# warn about). SessionStart can only advise (spike F4) — the hard block is the
# PreToolUse companion in sessions-enforce.py.
if is_plugin_mode():
    _plugin_root = get_plugin_root()
    # Pin the statusline into the per-machine, gitignored settings.local.json.
    # Claude Code does not expand ${CLAUDE_PLUGIN_ROOT} in a settings.json
    # statusLine command, so init can't write a portable plugin-relative path —
    # this hook resolves the absolute path and self-heals it across updates.
    _ensure_statusline_pinned(PROJECT_ROOT, _plugin_root)
    ensure_claude_dir_gitignored(PROJECT_ROOT)
    # Seed the per-project provider-token file (create-if-absent). Tokens live in
    # `.claude/state/provider-tokens.json` -- a per-project, user-authored, 0600,
    # git-ignored, PROTECTED file the AI cannot read. The OS-keychain userConfig
    # model was retired (it was global-per-plugin, so two projects could not use
    # different tokens). This writes a template with all provider keys empty for the
    # user to fill; it NEVER overwrites or deletes an existing (possibly filled) file.
    # Runs after the .claude/ ignore so the file lands in an already-ignored dir;
    # unconditional (a user may set tokens before /team-management:config ever
    # creates team-management/).
    ensure_provider_tokens_file()
    # Behavioral guidance (CLAUDE.tm.md / CLAUDE.tm.custom.md / CLAUDE.wiki.md) is
    # delivered through native @-includes in the project's CLAUDE.md, NOT injected
    # here. A one-shot SessionStart additionalContext injection fades out of a long
    # or /compact-ed session; a project-root CLAUDE.md and its @-imports are re-read
    # and re-injected after compaction (durable). The hook self-heals the delivery so
    # existing installs migrate without re-running init: refresh-on-change copies of
    # the plugin-owned files + an idempotent managed @-block in CLAUDE.md. Gated on
    # team-management/ existing so an un-opted-in project is never scaffolded; the
    # wrapper deploys BEFORE wiring so no @-target is ever dangling, and the task
    # TEMPLATE.md is deployed in the same gate (h-durable-guidance-via-claude-md).
    if (PROJECT_ROOT / 'team-management').is_dir():
        ensure_task_template_deployed(PROJECT_ROOT, _plugin_root)
        _wiki_enabled = False
        try:
            _cfg = PROJECT_ROOT / 'team-management' / 'config.json'
            if _cfg.exists():
                _wiki_enabled = bool(
                    json.loads(_cfg.read_text(encoding='utf-8')).get('wiki', {}).get('enabled', False)
                )
        # AttributeError/TypeError widen the guard: a malformed config.json (non-dict
        # root, or a non-dict `wiki` value) must leave _wiki_enabled=False, never crash
        # the hook so additionalContext is never emitted (code-review Note 1).
        except (OSError, ValueError, AttributeError, TypeError):
            pass
        ensure_guidance_deployed_and_wired(PROJECT_ROOT, _plugin_root, _wiki_enabled)
    # Boot-detector advisory.
    _legacy = detect_legacy_install(PROJECT_ROOT)
    if _legacy:
        context += (
            "\n\n[Boot Detector — ACTION REQUIRED] A legacy team-management install "
            "was detected alongside the plugin: " + ", ".join(_legacy["signals"]) + ". "
            "Both hook sets fire on every event, silently corrupting DAIC. Remove the "
            "legacy .claude/hooks/ directory and the team-management hook + mcpServers "
            "entries from .claude/settings.json (see the uninstall guide). Write tools "
            "are blocked until the legacy install is removed.\n"
        )

output = {
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": context
    }
}
print(json.dumps(output))