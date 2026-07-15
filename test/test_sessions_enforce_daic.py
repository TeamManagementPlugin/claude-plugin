#!/usr/bin/env python3
"""Regression tests for removal of the legacy `daic`-substring Bash shim.

The old sessions-enforce.py denied ANY discussion-mode Bash command whose text
contained the substring `daic` (a leftover from the retired `daic` CLI). The
read-only fast-path (lines ~502-530) short-circuits *fully* read-only commands,
so a bare `grep daic foo.py` slips past — but a command that is not recognised
as read-only reaches the shim and is falsely denied. Two extremely common shapes
hit it:

  - the literal `daic` command (not in the read-only whitelist), and
  - any otherwise-read-only command carrying a `2>/dev/null` stderr redirect,
    which trips BASH_WRITE_PATTERNS (`>\\s*[^>]`) and disables the fast-path.

These tests pin the post-removal contract:

  (1) the literal `daic` command is NOT denied with the shim's `[DAIC: Info]`;
  (2) a read-only `grep ... 2>/dev/null` containing `daic` is NOT denied;
  (3) a pure read-only `grep daic` stays allowed (guard, fast-path);
  (4) writing to .claude/state/daic-mode.json is STILL blocked (exit 2) by the
      independent PROTECTED_PATHS check, so removing the shim opens no hole.

Each test invokes sessions-enforce.py as a subprocess with a JSON stdin payload
(mimicking how Claude Code drives PreToolUse hooks).

Run with: python3 -m pytest test/test_sessions_enforce_daic.py -v
"""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = REPO_ROOT / "plugin" / "hooks" / "sessions-enforce.py"


def _make_project(tmp_path, *, daic_mode="discussion", task=None, branch=None,
                  init_git=False, subagent_depth=None, provider=None):
    """Build a minimal project tree for hook execution; return project root.

    subagent_depth: when set, writes .claude/state/subagent-depth.json {"depth": n}
      so in_subagent_context() reads True (n > 0) inside the hook.
    provider: when set, writes team-management/config.json with
      issue_tracking.provider = <provider> so check_provider_workflow_required()
      returns True for gitlab/jira/github (drives block_manual_task_archival).
    """
    state = tmp_path / ".claude" / "state"
    state.mkdir(parents=True)
    (tmp_path / "team-management" / "tasks").mkdir(parents=True)

    (state / "daic-mode.json").write_text(json.dumps({"mode": daic_mode}))
    (state / "current_task.json").write_text(json.dumps(
        {"task": task, "branch": branch, "services": [], "updated": "2026-06-22"}))

    if subagent_depth is not None:
        (state / "subagent-depth.json").write_text(
            json.dumps({"depth": subagent_depth}), encoding="utf-8")

    if provider is not None:
        (tmp_path / "team-management" / "config.json").write_text(
            json.dumps({"issue_tracking": {"provider": provider}}), encoding="utf-8")

    if task:
        body = (f"---\ntask: {task}\nbranch: {branch or ''}\n"
                f"status: in-progress\n---\n\n# Test task\n")
        (tmp_path / "team-management" / "tasks" / f"{task}.md").write_text(body)

    if init_git:
        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "--allow-empty", "-m", "init", "-q"],
                       cwd=str(tmp_path), check=True)
        if branch:
            subprocess.run(["git", "checkout", "-q", "-b", branch],
                           cwd=str(tmp_path), check=True)

    return tmp_path


def _run_hook(project_root, tool_name, tool_input):
    """Invoke sessions-enforce.py with a stdin payload; return (rc, out, err)."""
    payload = json.dumps({"tool_name": tool_name, "tool_input": tool_input})
    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=payload, capture_output=True, text=True,
        cwd=str(project_root), timeout=15,
    )
    return result.returncode, result.stdout, result.stderr


def _assert_not_shim_denied(rc, stdout, stderr):
    combined = stdout + stderr
    assert "[DAIC: Info]" not in combined, \
        f"legacy daic shim still firing: {combined!r}"
    assert '"deny"' not in stdout, f"command unexpectedly denied: {stdout!r}"
    assert rc == 0, f"expected allow (rc 0), got rc={rc}; out={stdout!r} err={stderr!r}"


def test_legacy_daic_command_not_denied(tmp_path):
    """The literal `daic` command (the retired CLI) reaches the shim because it
    is not in the read-only whitelist. After removal it must NOT be denied with
    the misleading `[DAIC: Info]` message — the shell will just report
    command-not-found, which is the correct signal."""
    project = _make_project(tmp_path, daic_mode="discussion",
                            task="m-x", branch="fix/x", init_git=True)
    rc, stdout, stderr = _run_hook(project, "Bash", {"command": "daic"})
    _assert_not_shim_denied(rc, stdout, stderr)


def test_readonly_grep_with_stderr_redirect_not_denied(tmp_path):
    """A read-only grep carrying `2>/dev/null` trips BASH_WRITE_PATTERNS, so the
    read-only fast-path is skipped and the command reaches the shim. This is the
    common real-world false-trigger; after removal it must NOT be denied."""
    project = _make_project(tmp_path, daic_mode="discussion",
                            task="m-x", branch="fix/x", init_git=True)
    rc, stdout, stderr = _run_hook(
        project, "Bash", {"command": "grep -rn daic . 2>/dev/null"})
    _assert_not_shim_denied(rc, stdout, stderr)


def test_pure_readonly_daic_grep_allowed(tmp_path):
    """Guard: a fully read-only command containing `daic` is allowed via the
    read-only fast-path (passes with or without the shim)."""
    project = _make_project(tmp_path, daic_mode="discussion",
                            task="m-x", branch="fix/x", init_git=True)
    rc, stdout, stderr = _run_hook(project, "Bash",
                                   {"command": "grep daic foo.py"})
    _assert_not_shim_denied(rc, stdout, stderr)


def test_bash_write_to_daic_mode_json_still_blocked(tmp_path):
    """Writing to .claude/state/daic-mode.json must STILL be blocked (exit 2)
    by the PROTECTED_PATHS check, independent of the removed shim."""
    project = _make_project(tmp_path, daic_mode="discussion",
                            task="m-x", branch="fix/x", init_git=True)
    rc, stdout, stderr = _run_hook(
        project, "Bash",
        {"command": "echo '{\"mode\":\"implementation\"}' > .claude/state/daic-mode.json"})
    assert rc == 2, f"expected exit 2 (blocked), got rc={rc}; err={stderr!r}"
    assert "[Protocol Engine]" in stderr, \
        f"expected PROTECTED_PATHS block marker, got: {stderr!r}"


def test_bash_write_to_config_session_flag_blocked(tmp_path):
    """SEC-006: the whole .claude/state prefix is protected, so the LLM cannot
    author config-session.flag and self-authorise the config intent-gate. The
    flag is NOT one of the legacy explicit PROTECTED_PATHS entries — it is caught
    by the new `.claude/state` prefix entry."""
    project = _make_project(tmp_path, daic_mode="discussion",
                            task="m-x", branch="fix/x", init_git=True)
    rc, stdout, stderr = _run_hook(
        project, "Bash",
        {"command": "echo live > .claude/state/config-session.flag"})
    assert rc == 2, f"expected exit 2 (blocked), got rc={rc}; err={stderr!r}"
    assert "[Protocol Engine]" in stderr, \
        f"expected PROTECTED_PATHS block marker, got: {stderr!r}"


def test_edit_config_session_flag_blocked(tmp_path):
    """SEC-006: the Edit tool on a .claude/state file is blocked too, not only
    Bash — the protected-path check runs first for every write tool."""
    project = _make_project(tmp_path, daic_mode="implementation",
                            task="m-x", branch="fix/x", init_git=True)
    flag = project / ".claude" / "state" / "config-session.flag"
    rc, stdout, stderr = _run_hook(
        project, "Edit",
        {"file_path": str(flag), "old_string": "", "new_string": "live"})
    assert rc == 2, f"expected exit 2 (blocked), got rc={rc}; err={stderr!r}"
    assert "[Protocol Engine]" in stderr, \
        f"expected PROTECTED_PATHS block marker, got: {stderr!r}"


# --- /team-management:init project-config whitelist (m-fix-init-settings-whitelist) ---
# /team-management:init writes <project>/.claude/settings.json (plugin enablement +
# statusLine) and <project>/CLAUDE.tm.custom.md (custom-rules stub). Both must be
# writable in ANY DAIC mode and WITHOUT an active task — init runs on a fresh project
# with the plugin already enforcing. The administrative whitelist exits (sys.exit 0)
# before both the discussion-mode block and branch enforcement, so a Write to either
# file is allowed. Matching is exact-resolved-path equality, so a `.bak` sibling or a
# non-whitelisted project-root file is NOT whitelisted. File paths are built absolute
# from the project root (Claude Code passes absolute file_path; the hook resolves both
# sides, so /var vs /private/var canonicalises consistently).


def test_init_settings_json_allowed_discussion_no_task(tmp_path):
    """init's .claude/settings.json write is allowed in discussion mode with no
    active task (the fresh-install scenario). Without the whitelist entry the
    discussion-mode block denies the Write tool with [DAIC: Tool Blocked] (rc 2)."""
    project = _make_project(tmp_path, daic_mode="discussion")
    target = str(project / ".claude" / "settings.json")
    rc, stdout, stderr = _run_hook(
        project, "Write", {"file_path": target, "content": "{}"})
    assert rc == 0, f"expected allow (rc 0), got rc={rc}; out={stdout!r} err={stderr!r}"


def test_init_settings_json_allowed_implementation_no_task(tmp_path):
    """Same file in implementation mode with no active task: the whitelist must
    bypass branch enforcement, which otherwise blocks all edits on a no-task tree
    with [DAIC: Task State Required] (rc 2). No git is initialised — the no-task
    branch block fires regardless (its git call is error-text-only, try/except)."""
    project = _make_project(tmp_path, daic_mode="implementation")
    target = str(project / ".claude" / "settings.json")
    rc, stdout, stderr = _run_hook(
        project, "Write", {"file_path": target, "content": "{}"})
    assert rc == 0, f"expected allow (rc 0), got rc={rc}; out={stdout!r} err={stderr!r}"


def test_init_custom_rules_stub_allowed_discussion_no_task(tmp_path):
    """init's CLAUDE.tm.custom.md stub write is allowed in discussion mode with no
    active task. A project-root .md is NOT covered by the documentation-mode .md
    allowance (that applies only in documentation mode), so the whitelist is the
    mechanism that permits it here."""
    project = _make_project(tmp_path, daic_mode="discussion")
    target = str(project / "CLAUDE.tm.custom.md")
    rc, stdout, stderr = _run_hook(
        project, "Write",
        {"file_path": target, "content": "# CLAUDE.tm.custom.md\n"})
    assert rc == 0, f"expected allow (rc 0), got rc={rc}; out={stdout!r} err={stderr!r}"


def test_init_whitelist_is_exact_not_prefix(tmp_path):
    """Exact-equality guard: a `.bak` sibling of a whitelisted file is NOT
    whitelisted — proves the match is resolved-path equality, not a prefix/substring
    match. Stays blocked by the discussion-mode block (rc 2)."""
    project = _make_project(tmp_path, daic_mode="discussion")
    target = str(project / ".claude" / "settings.json.bak")
    rc, stdout, stderr = _run_hook(
        project, "Write", {"file_path": target, "content": "{}"})
    assert rc == 2, f"expected block (rc 2), got rc={rc}; out={stdout!r} err={stderr!r}"
    assert "[DAIC: Tool Blocked]" in stderr, \
        f"expected discussion-mode block marker, got: {stderr!r}"


def test_init_whitelist_does_not_open_root_write_hole(tmp_path):
    """A non-whitelisted project-root file (foo.py) stays blocked in discussion
    mode with no active task — the new whitelist entry is narrow and does not open
    a general project-root write hole."""
    project = _make_project(tmp_path, daic_mode="discussion")
    target = str(project / "foo.py")
    rc, stdout, stderr = _run_hook(
        project, "Write", {"file_path": target, "content": "x = 1\n"})
    assert rc == 2, f"expected block (rc 2), got rc={rc}; out={stdout!r} err={stderr!r}"
    assert "[DAIC: Tool Blocked]" in stderr, \
        f"expected discussion-mode block marker, got: {stderr!r}"


# --- Subagent Bash bypass in documentation mode (h-fix-subagent-bash-daic-bypass) ---
# AI-provider wrapper subagents (codex-cli / agy-cli) shell out via Bash. The subagent
# bypass must cover Bash too, so their CLI calls are NOT blocked by documentation-mode
# enforcement — which is exactly what wrongly happened (brainstorm steps are all
# documentation mode). The universal guards (protected path, frozen path, manual
# archival) run BEFORE the bypass, so broadening it to all tools opens no hole.


def test_subagent_bash_allowed_in_documentation_mode(tmp_path):
    """A subagent's non-read-only Bash command (a codex CLI call whose prompt trips
    BASH_WRITE_PATTERNS via `>`) must be ALLOWED in documentation mode — the subagent
    bypass covers Bash. Without the fix it is blocked (rc 2) by the documentation-mode
    Bash block at [DAIC: Documentation Mode]."""
    project = _make_project(tmp_path, daic_mode="documentation", subagent_depth=1)
    rc, stdout, stderr = _run_hook(
        project, "Bash",
        {"command": 'codex exec -s read-only "summarize the diff > report"'})
    assert rc == 0, f"expected allow (rc 0), got rc={rc}; out={stdout!r} err={stderr!r}"


def test_nonsubagent_bash_still_blocked_in_documentation_mode(tmp_path):
    """The MAIN agent (no subagent depth) must STILL be blocked from a write Bash
    command in documentation mode — broadening the subagent bypass must not weaken
    main-thread documentation-mode enforcement."""
    project = _make_project(tmp_path, daic_mode="documentation")  # no subagent_depth
    rc, stdout, stderr = _run_hook(
        project, "Bash", {"command": "echo hi > out.txt"})
    assert rc == 2, f"expected block (rc 2), got rc={rc}; out={stdout!r} err={stderr!r}"
    assert "[DAIC: Documentation Mode]" in stderr, \
        f"expected documentation-mode block marker, got: {stderr!r}"


def test_subagent_bash_protected_path_still_blocked(tmp_path):
    """A subagent's Bash command targeting .claude/state must STILL be blocked — the
    protected-path check runs BEFORE the subagent bypass, so broadening the bypass
    does not open a hole to system state via subagent Bash."""
    project = _make_project(tmp_path, daic_mode="documentation", subagent_depth=1)
    rc, stdout, stderr = _run_hook(
        project, "Bash", {"command": "echo x > .claude/state/daic-mode.json"})
    assert rc == 2, f"expected block (rc 2), got rc={rc}; out={stdout!r} err={stderr!r}"
    assert "[Protocol Engine]" in stderr, \
        f"expected PROTECTED_PATHS block marker, got: {stderr!r}"


def test_subagent_edit_state_still_blocked(tmp_path):
    """A subagent EDITING a .claude/state file must STILL be blocked. The protected-
    path check fires first ([Protocol Engine]); the subagent .claude/state edit-guard
    (preserved, nested under the broadened bypass) is a resolved-path belt-and-
    suspenders behind it. Either way, a subagent cannot author system state."""
    project = _make_project(tmp_path, daic_mode="implementation", subagent_depth=1)
    flag = project / ".claude" / "state" / "config-session.flag"
    rc, stdout, stderr = _run_hook(
        project, "Edit",
        {"file_path": str(flag), "old_string": "", "new_string": "live"})
    assert rc == 2, f"expected block (rc 2), got rc={rc}; out={stdout!r} err={stderr!r}"
    assert "[Protocol Engine]" in stderr, \
        f"expected PROTECTED_PATHS block marker, got: {stderr!r}"


def test_subagent_bash_manual_archival_still_blocked(tmp_path):
    """A subagent's manual task archival (`mv … tasks/done/`) must STILL be blocked
    when a provider workflow is required. block_manual_task_archival was relocated to
    run BEFORE the subagent bypass precisely so the broadened bypass cannot let a
    subagent skip provider-workflow archival discipline (codex review finding)."""
    project = _make_project(tmp_path, daic_mode="implementation",
                            subagent_depth=1, provider="github")
    rc, stdout, stderr = _run_hook(
        project, "Bash",
        {"command": "mv team-management/tasks/m-x.md team-management/tasks/done/m-x.md"})
    assert rc == 2, f"expected block (rc 2), got rc={rc}; out={stdout!r} err={stderr!r}"
    assert "[TASK COMPLETION BLOCKED]" in stderr, \
        f"expected archival block marker, got: {stderr!r}"


def test_nonsubagent_manual_archival_still_blocked(tmp_path):
    """Relocation regression guard: the MAIN agent's manual archival must STILL be
    blocked after moving block_manual_task_archival up into the Bash guard block."""
    project = _make_project(tmp_path, daic_mode="implementation", provider="github")
    rc, stdout, stderr = _run_hook(
        project, "Bash",
        {"command": "mv team-management/tasks/m-x.md team-management/tasks/done/m-x.md"})
    assert rc == 2, f"expected block (rc 2), got rc={rc}; out={stdout!r} err={stderr!r}"
    assert "[TASK COMPLETION BLOCKED]" in stderr, \
        f"expected archival block marker, got: {stderr!r}"


# --------------------------------------------------------------------------
# .claude/state/provider-tokens.json is a SECRET file (keychain-token bridge):
# no Claude tool — main-thread or subagent — may read or write it, and the
# `.claude/./state` / `.claude//state` spelling bypasses are closed by
# _collapse_redundant_segments (h-fix-mcp-token-ondisk-bridge).
# --------------------------------------------------------------------------

_TOKENS_REL = ".claude/state/provider-tokens.json"


def test_read_provider_tokens_blocked(tmp_path):
    """The secret bridge file must be unreadable via the Read tool."""
    project = _make_project(tmp_path, daic_mode="discussion")
    rc, stdout, stderr = _run_hook(project, "Read", {"file_path": _TOKENS_REL})
    assert rc == 2, f"expected block (rc 2), got rc={rc}; err={stderr!r}"
    assert "[Protocol Engine]" in stderr, f"expected block marker, got: {stderr!r}"


def test_grep_provider_tokens_blocked(tmp_path):
    project = _make_project(tmp_path, daic_mode="discussion")
    rc, stdout, stderr = _run_hook(project, "Grep", {"path": _TOKENS_REL})
    assert rc == 2, f"expected block (rc 2), got rc={rc}; err={stderr!r}"
    assert "[Protocol Engine]" in stderr, f"expected block marker, got: {stderr!r}"


def test_edit_provider_tokens_blocked(tmp_path):
    project = _make_project(tmp_path, daic_mode="implementation")
    tokens = project / ".claude" / "state" / "provider-tokens.json"
    rc, stdout, stderr = _run_hook(
        project, "Edit",
        {"file_path": str(tokens), "old_string": "", "new_string": "x"})
    assert rc == 2, f"expected block (rc 2), got rc={rc}; err={stderr!r}"
    assert "[Protocol Engine]" in stderr, f"expected block marker, got: {stderr!r}"


def test_write_provider_tokens_blocked(tmp_path):
    project = _make_project(tmp_path, daic_mode="implementation")
    tokens = project / ".claude" / "state" / "provider-tokens.json"
    rc, stdout, stderr = _run_hook(
        project, "Write", {"file_path": str(tokens), "content": "x"})
    assert rc == 2, f"expected block (rc 2), got rc={rc}; err={stderr!r}"
    assert "[Protocol Engine]" in stderr, f"expected block marker, got: {stderr!r}"


def test_bash_cat_provider_tokens_blocked(tmp_path):
    project = _make_project(tmp_path, daic_mode="discussion")
    rc, stdout, stderr = _run_hook(
        project, "Bash", {"command": "cat .claude/state/provider-tokens.json"})
    assert rc == 2, f"expected block (rc 2), got rc={rc}; err={stderr!r}"
    assert "[Protocol Engine]" in stderr, f"expected block marker, got: {stderr!r}"


def test_read_dot_segment_spelling_blocked(tmp_path):
    """`.claude/./state/...` must NOT slip past the substring match — the
    _collapse_redundant_segments normalization closes this spelling bypass."""
    project = _make_project(tmp_path, daic_mode="discussion")
    rc, stdout, stderr = _run_hook(
        project, "Read", {"file_path": ".claude/./state/provider-tokens.json"})
    assert rc == 2, f"expected block (rc 2), got rc={rc}; err={stderr!r}"
    assert "[Protocol Engine]" in stderr, f"expected block marker, got: {stderr!r}"


def test_bash_double_slash_spelling_blocked(tmp_path):
    """`.claude//state/...` (redundant slash) is blocked post-normalization too."""
    project = _make_project(tmp_path, daic_mode="discussion")
    rc, stdout, stderr = _run_hook(
        project, "Bash", {"command": "cat .claude//state/provider-tokens.json"})
    assert rc == 2, f"expected block (rc 2), got rc={rc}; err={stderr!r}"
    assert "[Protocol Engine]" in stderr, f"expected block marker, got: {stderr!r}"


def test_subagent_read_provider_tokens_blocked(tmp_path):
    """A SUBAGENT (codex-cli/agy-cli) Read of the secret bridge must be blocked too —
    the protected-path check runs BEFORE the subagent bypass."""
    project = _make_project(tmp_path, daic_mode="documentation", subagent_depth=1)
    rc, stdout, stderr = _run_hook(project, "Read", {"file_path": _TOKENS_REL})
    assert rc == 2, f"expected block (rc 2), got rc={rc}; err={stderr!r}"
    assert "[Protocol Engine]" in stderr, f"expected block marker, got: {stderr!r}"


def test_subagent_bash_read_provider_tokens_blocked(tmp_path):
    project = _make_project(tmp_path, daic_mode="documentation", subagent_depth=1)
    rc, stdout, stderr = _run_hook(
        project, "Bash", {"command": "cat .claude/state/provider-tokens.json"})
    assert rc == 2, f"expected block (rc 2), got rc={rc}; err={stderr!r}"
    assert "[Protocol Engine]" in stderr, f"expected block marker, got: {stderr!r}"


def test_provider_tokens_blocked_even_under_workflow_bypass(tmp_path):
    """Workflow bypass disables WORKFLOW enforcement (DAIC/branch/protocol) but must
    NOT expose the secret token bridge — the secret-file guard runs BEFORE the bypass
    exit (codex review P1). The sanity check confirms bypass IS active by showing a
    normal protected state file (daic-mode.json) is allowed under it, proving the
    carve-out is targeted to the secret, not a blanket re-enable."""
    project = _make_project(tmp_path, daic_mode="implementation")
    (project / ".claude" / "state" / "workflow-bypass.json").write_text(
        json.dumps({"enabled": True}), encoding="utf-8")

    # Sanity: bypass is active -> a normal protected file slips through (rc 0).
    rc_norm, _, _ = _run_hook(
        project, "Read", {"file_path": ".claude/state/daic-mode.json"})
    assert rc_norm == 0, "bypass should be active (normal protected file allowed)"

    # But the SECRET bridge is STILL blocked — Read/Bash, dot-segment AND ".." spellings.
    # The `..` form is resolved via os.path.normpath in the structured-path guard
    # (codex review P1: structured paths are fully normalised before the bypass exit).
    for tool, ti in [
        ("Read", {"file_path": _TOKENS_REL}),
        ("Read", {"file_path": ".claude/./state/provider-tokens.json"}),
        ("Read", {"file_path": ".claude/state/../state/provider-tokens.json"}),
        ("Bash", {"command": "cat .claude/state/provider-tokens.json"}),
    ]:
        rc, _, stderr = _run_hook(project, tool, ti)
        assert rc == 2, f"{tool} {ti} not blocked under bypass: rc={rc}; err={stderr!r}"
        assert "[Protocol Engine]" in stderr, f"expected block marker, got: {stderr!r}"

    # P2 (no false positive): a sibling that merely shares the name as a prefix is NOT
    # blocked — the structured-path match is on a component boundary, not raw substring.
    rc_bak, _, _ = _run_hook(
        project, "Read", {"file_path": ".claude/state/provider-tokens.json.bak"})
    assert rc_bak == 0, "sibling .bak must not be false-blocked under bypass"


# ===========================================================================
# m-enforcement-and-git-hardening — documentation-mode tasks-root anchor
# ===========================================================================

def test_doc_mode_blocks_source_file_under_unrelated_tasks_dir(tmp_path):
    """A source file under ANY dir named `tasks` (app/tasks/worker.py) was
    editable in documentation mode via the loose `"/tasks/" in path` substring.
    Anchored to team-management/tasks/, it is now correctly BLOCKED."""
    project = _make_project(tmp_path, daic_mode="documentation")
    rc, stdout, stderr = _run_hook(
        project, "Edit",
        {"file_path": "app/tasks/worker.py", "old_string": "a", "new_string": "b"})
    assert rc == 2, f"source file under app/tasks/ wrongly editable in doc mode: {stderr!r}"
    assert "Documentation Mode" in (stdout + stderr)


def test_doc_mode_allows_task_dir_non_md_artifact(tmp_path):
    """A non-.md artefact under the REAL tasks root (a directory-task's
    results.tsv) stays editable in documentation mode via the anchored check."""
    project = _make_project(tmp_path, daic_mode="documentation")
    rc, stdout, stderr = _run_hook(
        project, "Edit",
        {"file_path": "team-management/tasks/m-x/results.tsv",
         "old_string": "a", "new_string": "b"})
    assert rc == 0, f"task-dir artefact wrongly blocked in doc mode: {stderr!r}"


def test_readonly_multitoken_command_allowed_discussion(tmp_path):
    """Wiring sanity: the extracted command_is_read_only helper is imported and
    used — a multi-token read-only command is fast-pathed (exit 0) in discussion
    mode. Guards against the helper extraction breaking the hook's import."""
    project = _make_project(tmp_path, daic_mode="discussion",
                            task="m-x", branch="fix/x", init_git=True)
    for cmd in ("git status --short", "sed -n '1,5p' f", "ls -la", "cd /tmp"):
        rc, _, stderr = _run_hook(project, "Bash", {"command": cmd})
        assert rc == 0, f"read-only {cmd!r} not allowed: rc={rc} err={stderr!r}"


def test_relative_path_from_subdir_still_enforces_branch(tmp_path):
    """A RELATIVE edit path issued from a subdirectory must still trigger branch
    enforcement. find_git_repo(file_path) previously returned None for a
    relative path walked from a non-repo-root cwd → enforcement silently off; the
    fix resolves file_path to an absolute dir (.resolve().parent) first. RED if
    reverted to find_git_repo(file_path)."""
    import os
    state = tmp_path / ".claude" / "state"
    state.mkdir(parents=True)
    (tmp_path / "team-management" / "tasks").mkdir(parents=True)
    (state / "daic-mode.json").write_text(json.dumps({"mode": "implementation"}))
    (state / "current_task.json").write_text(json.dumps(
        {"task": "m-x", "branch": "feature/expected", "services": [], "updated": "2026-07-04"}))
    (tmp_path / "team-management" / "tasks" / "m-x.md").write_text(
        "---\ntask: m-x\nbranch: feature/expected\nstatus: in-progress\n---\n\n# t\n")
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "--allow-empty", "-m", "init", "-q"],
                   cwd=str(tmp_path), check=True)
    # HEAD stays on the git-default branch (main/master), NOT feature/expected.
    subdir = tmp_path / "src"
    subdir.mkdir()
    payload = json.dumps({
        "tool_name": "Edit",
        "tool_input": {"file_path": "foo.py", "old_string": "a", "new_string": "b"}})
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(tmp_path)}
    env.pop("CLAUDE_PLUGIN_ROOT", None)  # avoid plugin-mode boot-detector path
    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)], input=payload,
        capture_output=True, text=True, cwd=str(subdir), env=env, timeout=15)
    assert result.returncode == 2, (
        f"relative path from subdir skipped branch enforcement: "
        f"rc={result.returncode} out={result.stdout!r} err={result.stderr!r}")


# ===========================================================================
# Fail-open hardening (h-fix-daic-enforcement-fail-open)
# ===========================================================================
# Only exit code 2 blocks a PreToolUse tool call; any uncaught exception exits 1
# (non-blocking) and SILENTLY disables all enforcement. These tests pin the
# hybrid failure contract: foreseeable corruption (unreadable / non-dict state)
# fails CLOSED via restrictive defaults; the hook never crashes with a traceback.

def _run_hook_stdin(project_root, raw_stdin):
    """Run sessions-enforce.py with arbitrary raw stdin. Pins CLAUDE_PROJECT_DIR
    to the tmp project and drops CLAUDE_PLUGIN_ROOT so the pytest environment
    (which may carry both when the suite is run inside a live plugin session)
    cannot leak the real repo root or trip the plugin-mode boot detector."""
    import os
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project_root)}
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=raw_stdin, capture_output=True, text=True,
        cwd=str(project_root), env=env, timeout=15)
    return result.returncode, result.stdout, result.stderr


def _assert_no_traceback(stderr):
    assert "Traceback (most recent call last)" not in stderr, \
        f"enforcement hook crashed with a traceback (silent fail-open): {stderr!r}"


def test_empty_stdin_does_not_crash(tmp_path):
    """Empty stdin: an unguarded json.load raises → exit 1 → silent fail-open.
    Guarded, it degrades to {} → no tool matched → exit 0, no traceback."""
    project = _make_project(tmp_path, daic_mode="discussion", task="m-x",
                            branch="fix/x", init_git=True)
    rc, _, stderr = _run_hook_stdin(project, "")
    _assert_no_traceback(stderr)
    assert rc == 0, f"empty stdin: expected exit 0, got rc={rc} err={stderr!r}"


def test_malformed_stdin_does_not_crash(tmp_path):
    """Malformed JSON payload → guarded degrade to {} → exit 0, no traceback."""
    project = _make_project(tmp_path, daic_mode="discussion", task="m-x",
                            branch="fix/x", init_git=True)
    rc, _, stderr = _run_hook_stdin(project, "{ not valid json")
    _assert_no_traceback(stderr)
    assert rc == 0, f"malformed stdin: expected exit 0, got rc={rc} err={stderr!r}"


def test_corrupt_daic_mode_json_fails_closed_on_edit(tmp_path):
    """daic-mode.json with invalid UTF-8 bytes: check_daic_mode_raw would raise
    UnicodeDecodeError → exit 1 → fail-OPEN on the current code. Hardened, it
    degrades to 'discussion' so the Edit is BLOCKED (exit 2) — fail CLOSED."""
    project = _make_project(tmp_path, daic_mode="discussion", task="m-x",
                            branch="fix/x", init_git=True)
    (project / ".claude" / "state" / "daic-mode.json").write_bytes(
        b"\xff\xfe garbage \x00 not utf8")
    payload = json.dumps({"tool_name": "Edit", "tool_input": {
        "file_path": "src/foo.py", "old_string": "a", "new_string": "b"}})
    rc, _, stderr = _run_hook_stdin(project, payload)
    _assert_no_traceback(stderr)
    assert rc == 2, \
        f"corrupt daic-mode.json must fail CLOSED (block edit): rc={rc} err={stderr!r}"


def test_nondict_daic_mode_json_fails_closed_on_edit(tmp_path):
    """A valid-but-non-dict daic-mode.json (JSON list): data.get() → AttributeError
    on the current code. The isinstance(dict) guard degrades to 'discussion' →
    Edit BLOCKED (exit 2)."""
    project = _make_project(tmp_path, daic_mode="discussion", task="m-x",
                            branch="fix/x", init_git=True)
    (project / ".claude" / "state" / "daic-mode.json").write_text(
        json.dumps(["not", "a", "dict"]), encoding="utf-8")
    payload = json.dumps({"tool_name": "Edit", "tool_input": {
        "file_path": "src/foo.py", "old_string": "a", "new_string": "b"}})
    rc, _, stderr = _run_hook_stdin(project, payload)
    _assert_no_traceback(stderr)
    assert rc == 2, \
        f"non-dict daic-mode.json must fail CLOSED (block edit): rc={rc} err={stderr!r}"


def test_corrupt_current_task_json_fails_closed_on_edit(tmp_path):
    """current_task.json with invalid UTF-8 bytes: get_task_state would raise
    UnicodeDecodeError → exit 1 → fail-OPEN. Hardened, it degrades to null-task;
    with DAIC in implementation mode, branch enforcement's fail-safe BLOCKS the
    edit (exit 2)."""
    project = _make_project(tmp_path, daic_mode="implementation", init_git=True)
    (project / ".claude" / "state" / "current_task.json").write_bytes(
        b"\xff\xfe not utf8 \x00")
    payload = json.dumps({"tool_name": "Edit", "tool_input": {
        "file_path": "src/foo.py", "old_string": "a", "new_string": "b"}})
    rc, _, stderr = _run_hook_stdin(project, payload)
    _assert_no_traceback(stderr)
    assert rc == 2, \
        f"corrupt current_task.json must fail CLOSED: rc={rc} err={stderr!r}"


def test_unforeseen_internal_error_fails_open_loudly(tmp_path):
    """The top-level main() backstop: a genuinely UNFORESEEN crash — an embedded
    null byte in file_path makes Path.resolve() raise ValueError deep in branch
    enforcement, a path none of the reader guards cover — must fail OPEN LOUDLY:
    exit 0 with a stderr breadcrumb, never a traceback, never a session-bricking
    exit 2 (the matcher covers Read|Grep|Bash too)."""
    project = _make_project(tmp_path, daic_mode="implementation", task="m-x",
                            branch="fix/x", init_git=True)
    payload = json.dumps({"tool_name": "Edit", "tool_input": {
        "file_path": "foo\x00.py", "old_string": "a", "new_string": "b"}})
    rc, _, stderr = _run_hook_stdin(project, payload)
    _assert_no_traceback(stderr)
    assert rc == 0, f"backstop must fail OPEN (exit 0): rc={rc} err={stderr!r}"
    assert "[sessions-enforce] internal error" in stderr, \
        f"backstop breadcrumb missing: {stderr!r}"
