# MCP Module CLAUDE.md

## Purpose
Provides Model Context Protocol (MCP) server exposing issue tracking, git operations, code review, and DAIC mode management as first-class Claude tools with automatic multi-provider routing.

## Narrative Summary

The MCP module implements a FastMCP-based server that exposes team-management functionality as native MCP tools, making them discoverable and usable by Claude agents. MCP tools appear directly in Claude's tool list and can be invoked like any other tool.

The server was refactored from a 3,458-line monolith into a clean modular architecture with 42 MCP tools organized across 8 tool modules. This decomposition improves maintainability, testability, and enables independent development of tool categories.

The server uses a **zero-duplication architecture**: it imports provider utilities (gitlab_utils.py, jira_utils.py, github_utils.py) from the plugin's hooks dir rather than maintaining separate copies. `get_hooks_path()` resolves that dir plugin-root-first — `${CLAUDE_PLUGIN_ROOT}/hooks`, then `.claude/hooks` (legacy), then the dev `plugin/hooks` checkout. This ensures hook-based auto-sync and MCP tool operations run the same underlying API code.

**Import Architecture**: The server uses absolute imports with a sys.path bootstrap because MCP runs server.py as a script, not as a Python module. This means relative imports (from .core.project) would fail. Instead, server.py adds its directory to sys.path at startup, enabling absolute imports like `from core.project import ...`.

## Cold-Start Bootstrap (h-hook-port-boot-detector)

`bootstrap_mcp.py` is the MCP entry point named in `plugin/.mcp.json` (`python3 ${CLAUDE_PLUGIN_ROOT}/mcp/bootstrap_mcp.py`). It runs under the **system** python before the plugin venv exists, so it is pure stdlib — it must NOT import `server.py` or anything under the venv. On each cold start it:
- resolves `${CLAUDE_PLUGIN_DATA}` (the venv lives there because PLUGIN_ROOT is replaced on update — spike F3);
- rebuilds the venv when `${CLAUDE_PLUGIN_DATA}/venv` is missing or the recorded sha256 of `requirements.lock` differs (`needs_build`), via `python -m venv` + `pip install --require-hashes -r requirements.lock`, writing the hash file **last** so a crashed install self-heals on the next run;
- serializes the build behind an interprocess `build_lock` (fcntl/msvcrt, OSError → no-op degrade) with a double-checked `needs_build()` inside the lock, so two concurrent cold-starts can't clobber the shared venv. **Contention-only breadcrumb** (l-fix-subprocess-timeout-hardening): a non-blocking probe (`LOCK_EX|LOCK_NB` / `LK_NBLCK`) runs first — an uncontended build acquires silently; only a genuine contention errno (`EAGAIN`/`EWOULDBLOCK`/`EACCES`) prints `[bootstrap_mcp] waiting for a concurrent venv build to finish…` before blocking on `LOCK_EX`, while a locking-unsupported errno (`ENOSYS`/`ENOTSUP`) re-raises into the outer `except OSError: pass` degrade path with no misleading breadcrumb;
- guards Python >= 3.10 and verifies pip is present in the new venv;
- **captures all build subprocess output** (m-statusline-and-test-infra) **and bounds each build step** (l-fix-subprocess-timeout-hardening): the `venv` / `pip --version` / `pip install` calls run through the `_run_capture(args, label)` helper with `capture_output=True` (no `check=True`) and a generous `BUILD_STEP_TIMEOUT = 600` outer bound; captured stdout+stderr surface on **stderr** (never the MCP stdout channel — pip bytes on stdout are pre-handshake garbage that can break strict clients); any non-zero return exits with the captured diagnostic, and a `subprocess.TimeoutExpired` writes `[bootstrap_mcp] <label> timed out after 600s — check network/proxy` + `sys.exit(1)` (pip already carries its own 15s socket timeout + 5 retries — this is the belt-and-suspenders ceiling for retry-exhaustion pathologies);
- **guards a missing `requirements.lock`** (m-statusline-and-test-infra): when a build is required (`needs_build` true) but `LOCKFILE` is absent, exits with an actionable "requirements.lock missing — reinstall/repair the plugin" message BEFORE `build_lock`/pip (was a bare `pip install -r <missing>` CalledProcessError). Scoped to the build path — an already-current venv (fast path) needs no lockfile;
- hands off to the venv python running `server.py` via `os.execv` (POSIX) / `subprocess.run`+`sys.exit(rc)` (Windows) — BEFORE any MCP handshake, so replacing the process is safe (unlike a mid-session exec).

**Windows venv-staleness limitation** (documented in `needs_build`, m-statusline-and-test-infra): on POSIX the venv python is a symlink, so a system-Python upgrade breaks it and forces a rebuild; on Windows `python.exe` is a COPY, so a system-Python upgrade does NOT invalidate the venv (staleness is keyed only on the lockfile hash). Benign — the copied interpreter keeps working; delete `${CLAUDE_PLUGIN_DATA}/venv` to force a rebuild.

It is the launcher, not an MCP tool — the 42-tool inventory and the `## MCP Tools (N total)` header are unaffected. Real cold-start sim verified (cold build installs the hashed lockfile + execs; 2nd run fast-paths). Test: `test/test_bootstrap_mcp.py`. **Windows launcher (resolved — h-fix-windows-plugin-launch):** the manifests intentionally keep the literal `python3` token — it is correct on macOS/Linux and MUST NOT be changed to `python`/`py` (there is no real `python` on modern macOS, only an interactive alias). Windows has no `python3.exe`, so `/team-management:init` §0 provisions one by copying `python.exe` → `python3.exe` into the Python install dir. A real `.exe` (not a `.cmd` shim) is required because Claude Code direct-spawns the MCP `command` and Windows cannot direct-spawn a `.cmd` without a shell. Confirmed on Windows: a bare `.py` path is open-not-run, `py <script>` works, `python3` is absent.

## Architecture

### Modular Design (v0.6.0)
The server was refactored from a 3,458-line monolith to a clean modular architecture:

```
plugin/mcp/
    server.py              # Entry point (imports and registration only)
    core/                  # Shared infrastructure
        __init__.py        # Package exports
        config.py          # Configuration loading, caching, provider detection
        project.py         # Project root detection, task file utilities
    helpers/               # Utility functions
        __init__.py        # Package exports
        code_review_utils.py # URL parsing, branch validation, context gathering
    tools/                 # MCP tool implementations (42 tools across 8 modules)
        __init__.py        # Package exports
        issue_tracking.py  # 14 issue tracking tools
        protocol.py        # 11 protocol engine tools
        code_review.py     # 4 code review tools
        git_operations.py  # 4 git/MR tools
        daic.py            # 3 DAIC mode switching tools
        notifications.py   # 3 notification tools
        release.py         # 1 release creation tool
        config.py          # 2 config tools
```

**Benefit**: Clear separation of concerns, independent testability, maintainable modules.

### Tool Registration Pattern
Each tool module exports a `register_tools(mcp)` function that registers tools with the FastMCP server instance.

Server entry point (server.py:65-71) imports and calls each registration function:
- `daic.register_tools(mcp)`
- `release.register_tools(mcp)`
- `git_operations.register_tools(mcp)`
- `issue_tracking.register_tools(mcp)`
- `code_review.register_tools(mcp)`
- `protocol.register_tools(mcp)`
- `notifications.register_tools(mcp)`
- `config.register_tools(mcp)` — 2 config tools (config_get, config_update); added in m-config-mcp-flow

### Import Strategy (core/project.py — `get_hooks_path()`)
The server employs dynamic path resolution to import provider utilities from their canonical location. Updated for the three-root model (h-plugin-foundation, commit 2): `get_hooks_path()` walks an ordered candidate list and returns the first directory that exists AND contains `gitlab_utils.py`:

1. **Plugin install** (read-only): `CLAUDE_PLUGIN_ROOT` env (set by Claude Code for plugin processes) → `<PLUGIN_ROOT>/hooks` — tried FIRST when the env var is present.
2. **Deployed copy in the project**: `<project_root>/.claude/hooks` (installer layout / legacy single-source-of-truth).
3. **Dev / source checkout**: `Path(__file__).resolve().parent.parent.parent / 'hooks'` — this file is `plugin/mcp/core/project.py`, so three `.parent`s == `plugin/hooks`.
4. **Legacy dev layout** relative to project root: `<project_root>/plugin/hooks`.

If none of the candidates qualifies, raises a descriptive exception ("Provider utilities not found in the plugin root, .claude/hooks, or plugin/hooks"). **Project root detection** is unchanged: `CLAUDE_PROJECT_DIR` env first, then marker-file search. **Single Source of Truth**: no file copying — provider utils are imported from whichever of the three roots resolves. This keeps a single source of truth: hook-driven auto-sync and MCP tool operations always run the same provider code (no version skew).
## Module Structure

### Core Server Entry Point
- `server.py:1-86` - FastMCP server entry point
  - Imports core/project.setup_provider_imports() to initialize provider access
  - Creates FastMCP("team-management") instance
  - Imports and calls register_tools() from each tool module
  - Provides main() entry point for MCP server execution

### Core Infrastructure (core/)
- `config.py:1-196` - Configuration management
  - `load_config()` - Load config.json with mtime-based cache invalidation. Uses ONE guarded `stat()` (missing/unreadable → the `-1.0` sentinel, distinguishable from any real mtime ≥ 0) and runs the invalidation comparison on EVERY call, so an existence change in BOTH directions is caught — a `config.json` created after a first (file-absent) load in the fresh-install flow, or one later deleted. Every cache state stamps `_config_mtime` (`-1.0` when absent, the file's own mtime on success AND on the malformed-file exception path), closing the dual-`None` gate that permanently poisoned `{}` for the server lifetime (m-fix-mcp-config-cache-poisoning)
  - `reload_config()` - Force cache invalidation and clear provider singletons
  - `detect_provider()` - Detect active provider (gitlab/jira/github) from config; calls `load_config()` BEFORE the `_provider` cache check so mtime-based invalidation (which clears `_provider` via `reload_config`) actually runs — `issue_tracking.provider` changes are picked up without an MCP server restart (m-fix-mcp-git-review-tooling)
  - `get_provider_api()` - Get provider-specific API class instance
  - `get_provider_sync()` - Get provider-specific sync class instance
- `project.py:1-128` - Project root and path utilities
  - `get_project_root()` - Find project root via env var or marker search
  - `get_hooks_path()` - Locate provider utilities directory across the three-root model (h-plugin-foundation commit 2): `CLAUDE_PLUGIN_ROOT`/hooks → `.claude/hooks` → dev `plugin/hooks` (via `__file__`) → legacy `<project>/plugin/hooks`; first candidate containing `gitlab_utils.py` wins, else raises
  - `setup_provider_imports()` - Add hooks directory to sys.path
  - `_import_from_hooks(module_name)` (line 96, l-structural-refactors) - Single helper that ensures the hooks dir is on `sys.path` (`setup_provider_imports()`), then `importlib.import_module(module_name)` and returns the module. Replaces the scattered `setup_provider_imports(); from <module> import <names>` idiom — call sites do `mod = _import_from_hooks("shared_state")` and read `mod.PROJECT_ROOT`, etc. Adopted by `tools/protocol.py` and `tools/notifications.py`.
  - `find_task_file()` - Locate task file by name in team-management/tasks/
  - `get_task_relative_path()` - Get relative path from tasks directory

### Helper Modules (helpers/)
- `code_review_utils.py:1-439` - Code review utilities
  - `validate_branch_name()` - Validate branch name for command injection. **Now a lazy-import delegator** to the shared hooks single-source-of-truth `git_operations.validate_branch_name` (l-fix-security-hardening-residuals) — `from git_operations import validate_branch_name as _shared` inside the function, with an identical inline fallback (`^[a-zA-Z0-9/._-]+$` + leading-dash reject) on `ImportError` (a standalone import before the server puts the hooks dir on `sys.path`). Character class is `^[a-zA-Z0-9/._-]+$` — dot included so dotted branches like `release/1.0.0` validate — plus an explicit leading-dash rejection (a leading `-` would be parsed as a git option: option-injection guard). Replaced the former duplicate validator (m-fix-mcp-git-review-tooling)
  - `parse_mr_pr_url()` - Parse MR/PR URLs to extract provider and ID. Both GitHub patterns are anchored `^https?://` and matched with `re.match` (was `re.search`), so a forwarded path like `https://evil.com/forward/github.com/owner/repo/pull/123` no longer mis-parses as github.com; scheme-less URLs intentionally do not parse (provider `web_url` fields always carry a scheme); ReDoS depth guard (`url.count('/') > 20`) kept (m-fix-mcp-git-review-tooling)
  - `extract_code_review_from_notes()` - Find code review in MR notes
  - `fetch_mr_pr_by_url()` - Fetch MR/PR metadata from provider API
  - `prepare_review_environment()` - Switch branch, stash changes for review. Hardened (l-fix-subprocess-timeout-hardening): the three LOCAL ops carry timeouts (branch 15s, status 15s, stash push 30s) alongside the pre-existing network-op timeouts (fetch/checkout/pull 30/10/30), and a dedicated `except subprocess.TimeoutExpired` appends `"Git operation timed out (remote unreachable or a git hook hung)"` instead of the generic "Unexpected error"
  - `restore_git_environment()` (lines 286-371) - Restore original branch and unstash. Hardened (m-fix-mcp-git-review-tooling): timeouts on all three git calls (checkout 10s, stash list 10s, stash pop 30s) with an explicit `subprocess.TimeoutExpired` catch carrying a manual-resolution message (`git stash list` + `git stash pop <stash@{N}>` by hand); pops the EXACT `stash@{N}` ref parsed from the `git stash list` line matching "Temporary stash for code review" instead of a bare `git stash pop`, so an interloper stash pushed by another process between prepare and restore is never popped; success formula is `restored and (not was_stashed or unstashed)` — a failed or missing stash pop is no longer reported as success (silent data-loss fix)

### Tool Modules (tools/)
- `issue_tracking.py:1-993` - 14 issue tracking tools
  - `issue_status` - Show provider configuration and linked tasks
  - `config_issue_tracking_status` - Get config for protocol decisions
  - `config_code_review_enforcement` - Get code review warning config
  - `issue_read` - Import issue as Claude task (supports update mode)
  - `issue_create` - Create provider issue from task
  - `issue_update` - Update issue title, description, status, labels
  - `issue_sync` - Sync task status to linked issue
  - `issue_push` - Push task content changes to issue
  - `issue_link` - Link existing task to issue
  - `issue_unlink` - Remove issue link from task
  - `issue_comment` - Add comment to issue
  - `issue_set_status` - Set issue status with optional comment
  - `issue_api` - Direct provider API access (advanced)
  - `issue_dependency` - Manage issue dependencies (Gitea + GitHub same-repo)
- **MR/PR/release methods route through manager classes** (h-fix-mcp-manager-split-and-notebookedit): after the provider split, `add_mr_comment` / `get_merge_request` / `find_merge_request_by_branch` / `get_mr_notes` / `update_merge_request` / `create_merge_request` (GitLab MR), `create_release` (GitLab/GitHub release), and `find_pull_request_by_branch` (GitHub PR) live ONLY on `GitLabMRManager` / `GitLabReleaseManager` / `GitHubPRManager` / `GitHubReleaseManager` — NOT on bare `GitLabAPI()`/`GitHubAPI()`. These tool modules construct the manager wrapping the API (`GitLabMRManager(gitlab).get_merge_request(...)`; classes re-exported from `gitlab_utils`/`github_utils`). Calling a moved method on a bare API raises `AttributeError`, swallowed by the outer `except Exception`, silently breaking the tool. `github.add_comment(...)` (PR comments) and `github._make_request(...)` are NOT moved — they stay on `GitHubAPI`. Regression: `test/test_mcp_manager_split.py` drives each tool fn → real manager with mocked HTTP.
- `code_review.py:1-646` - 4 code review tools
  - `merge_request_comment` - Add comment to GitLab MR (via `GitLabMRManager.add_mr_comment`)
  - `pull_request_comment` - Add comment to GitHub PR (via `GitHubAPI.add_comment` — not a moved method)
  - `code_review` - Run automated code review workflow (find-MR/PR-by-branch via the managers)
  - `fetch_mr_review` - Fetch existing review from MR (via `GitLabMRManager.get_merge_request` / `get_mr_notes`)
- `git_operations.py:1-301` - 4 git/MR tools
  - `git_commit` - Commit changes with message validation
  - `git_push` - Push branch to remote with upstream tracking
  - `merge_request_create` - Create GitLab MR linked to task
  - `merge_request_update` - Update existing MR
- `daic.py` - 3 DAIC mode switching tools
  - `daic_mode_switch_discussion` - Switch to discussion mode
  - `daic_mode_switch_implementation` - Switch to implementation mode
  - `daic_mode_switch_documentation` - Switch to documentation mode
- `notifications.py` - 3 notification tools
  - `notify_user` - Send a notification to the user (e.g. Telegram)
  - `notification_status` - Report whether notifications are configured
  - `notification_discover_telegram_chats` - Discover Telegram chats (getMe + getUpdates) for the config flow, or send a confirmation ping to a chat id
- `protocol.py` - 11 protocol engine tools
  - Imports hooks modules (`shared_state`, `protocol_engine`) via the shared `_import_from_hooks` helper (l-structural-refactors). The `_check_enabled` config read was fixed to a single canonical path (`shared_state.PROJECT_ROOT / "team-management" / "config.json"`) — a self-contradicting duplicate config-path loop was removed.
  - `protocol_list`, `protocol_current`, `protocol_advance`, `protocol_goto`, `protocol_log`, `protocol_abort`, `protocol_save_note`, `protocol_available_funcs`
  - `protocol_customize(protocol_name, force=False)` + `protocol_check_drift(acknowledge=False)` — protocol-customization pair (bootstrap-copy a system protocol into `custom/` + reconcile upstream drift after a reinstall); thin wrappers around `ProtocolEngine.customize_protocol` / `ProtocolEngine.check_drift`. Surfaced by the `/team-management:custom-protocol-create` and `/team-management:custom-protocol-update-after-reinstall` slash commands.
  - `protocol_start(protocol_name, task=None, resume_force_safe=False)` (`tools/protocol.py:55-77`) — thin wrapper around `ProtocolEngine.start_protocol`. The `resume_force_safe: bool = False` kwarg (added in h-optimize-protocol-engine T2) is forwarded verbatim to the engine. When the same optimize protocol is already active with `loop_iteration > 0`, the engine resumes at the current step and runs a credential scan over `results.tsv` last 10 rows + `resume-stdout-tail.txt` last 100 KB / 1000 lines (AWS / JWT / OAuth / GitHub / GitLab / Slack token regexes). On match, the resume aborts unless `resume_force_safe=true` is passed; the bypass is recorded in the audit log. Best-effort speed-bump, not a security control.
- `release.py:1-160` - 1 release tool
  - `release_create` - Create release on GitLab/GitHub
- `config.py` - 2 config tools (m-config-mcp-flow)
  - `config_get` - Read-only config.json snapshot with sensitive values masked (ungated); also returns a `schema` catalog of every settable key
  - `config_update` - Gated, schema-validated writer for non-secret config (intent-gate flag + SEC-007 URL validation + sensitive-key reject + tracked-check + gitignore-ensure + atomic merge-write)

### Imported Dependencies (from .claude/hooks/)
- `gitlab_utils.py` - GitLab API wrapper (imported, not copied)
- `jira_utils.py` - Jira API wrapper (imported, not copied)
- `github_utils.py` - GitHub / Gitea API wrapper (imported, not copied)
- `issue_provider_base.py` - Base provider interface with find_task_file()
- `shared_state.py` - DAIC state management utilities

### Key Components

#### Project Root Detection (core/project.py)
- Environment variable detection (CLAUDE_PROJECT_DIR) — env-first, matching `shared_state.get_project_root`
- Marker-based search fallback (.git, team-management, pyproject.toml). The pip-era `'sessions'` marker was removed (m-statusline-and-test-infra) — that directory no longer exists post-plugin-conversion.
- **Documented divergence from `shared_state.get_project_root`**: this module is the bootstrap that puts the hooks dir on `sys.path` so `shared_state` can be imported, so it CANNOT delegate (chicken-and-egg). Both are env-first on `CLAUDE_PROJECT_DIR` (asserted equal by `test/test_mcp_core.py::test_project_root_matches_shared_state`); only the cwd-walk fallback marker set differs.
- Fallback to current working directory
- Cached result for performance (`_project_root`)

#### Provider Import Logic (core/project.py — `get_hooks_path()`)
- Three-root candidate walk (h-plugin-foundation commit 2): plugin install (`CLAUDE_PLUGIN_ROOT`/hooks) → deployed `.claude/hooks/` → dev `plugin/hooks/` (via `__file__`) → legacy `<project>/plugin/hooks/`
- First candidate that exists AND contains `gitlab_utils.py` wins
- Raises descriptive exception if no candidate qualifies
- Called once at server startup via setup_provider_imports()

#### Configuration Management (core/config.py load_config)
- Automatic cache invalidation on config file **existence-or-mtime** change (one guarded `stat()`; the `-1.0` sentinel marks a missing/unreadable file so a create-after-first-load OR a delete-after-load both trip the invalidation — not only an mtime change while the file exists)
- Fallback to empty config if file doesn't exist (stamps `_config_mtime = -1.0`, so the empty result is NOT cached for the process lifetime — the fresh-install poisoning bug, m-fix-mcp-config-cache-poisoning)
- Malformed config → `{}` but caches the file's own mtime, so a stably-broken file is not re-parsed every call yet self-heals once fixed (new mtime)
- Thread-safe by the single-threaded asyncio stdio server (no `await` in `load_config`); global-variable cache
- Silent failure to avoid breaking STDIO protocol

#### Provider Routing (core/config.py:83-196)
- `detect_provider()` checks issue_tracking.provider setting
- `load_config()` is hoisted above the `_provider` cache check (lines 94-100) so mtime-based cache invalidation can clear a stale provider singleton — provider changes in config.json take effect without restarting the MCP server (m-fix-mcp-git-review-tooling)
- Falls back to checking individual provider.enabled flags
- `get_provider_api()` returns GitLabAPI, JiraProvider, or GitHubAPI
- `get_provider_sync()` returns GitLabTaskSync, JiraTaskSync, or GitHubTaskSync
- Singleton pattern for GitHub/Gitea to maintain label cache

## MCP Tools (42 total)

### Issue Tracking Tools (14 tools in tools/issue_tracking.py)

#### issue_status()
- Shows active provider configuration and linked tasks
- Returns provider-specific details (base URL, project info)
- Lists all task-issue mappings with sync metadata
- Provides setup instructions if provider not configured
- **File-aware token detection** (m-config-mcp-flow; per-project file in m-per-project-provider-tokens): `has_token` / the `missing` list resolve the token via `shared_state.resolve_provider_token(provider, provider_cfg.get("api_token"))` (per-project `.claude/state/provider-tokens.json` → config.json), matching the provider API wrappers — otherwise a token present only in the tokens file would report "missing" here while the actual provider calls succeed
- **MR/PR-only mappings are skipped** (h-fix-gitlab-issue-status-keyerror): the gitlab/github loops read the issue id via `.get()` and `continue` past mappings that have no `gitlab_issue_id` / `issue_id` (i.e. MR-only / PR-only mappings written by `create_merge_request_for_task` / `create_pull_request_from_task`). This keeps `tasks` meaning "issue-linked tasks" — the contract the task protocol's issue-linking validation relies on — and avoids the prior `KeyError` on the bare subscript. `linked_tasks` is recomputed as `len(result["tasks"])` after the loop so the count never drifts from the listed tasks.
- **GitLab loop keys on `gitlab_issue_iid`, not `gitlab_issue_id`** (m-fix-issue-tracking-integration-robustness): the gitlab loop now reads the project iid (`mapping.get('gitlab_issue_iid')`) for the `gitlab:<n>` display id, the `issue_number` field, AND the MR-only skip guard. The project iid is the round-trip handle — it feeds back into `issue_set_status` / `issue_comment` / `issue_read`, which all use the `/projects/X/issues/{iid}` REST path; the global `gitlab_issue_id` 404s there. MR-only mappings lack `gitlab_issue_iid` too, so keying the skip on it still drops them. The github (keys on `issue_id` = path) and jira (keys on `jira_issue_key`) loops are unaffected.

#### config_issue_tracking_status()
- Get issue tracking configuration for protocol decision-making
- Returns provider config, auto_sync setting, configuration completeness
- Use case: Task creation protocol checking if issue is required

#### config_code_review_enforcement(task_content=None)
- Get code review warning enforcement configuration
- Optional task_content parameter for warning analysis
- Returns enforcement_mode (strict/relaxed), guidance language

#### issue_read(issue_id_or_url, update_mode=False)
- Imports issue from active provider as Claude task
- Accepts: numeric IDs (GitLab/GitHub), issue keys (Jira), or full URLs
- update_mode=True: Re-fetch and update existing task file
- Creates task file with proper formatting and metadata

#### issue_create(task_name)
- Creates provider issue from Claude task
- Parses task file for title, description, context
- Formats content appropriately for provider
- Returns provider-prefixed ID (gitlab:123, jira:PROJ-456)
- **GitLab returns the project iid** (m-fix-issue-tracking-integration-robustness): the GitLab path calls `create_gitlab_issue_from_task`, which now returns `issue['iid']` (project iid, the round-trip handle used by `issue_set_status` / `issue_comment` / `issue_read`) rather than the global `issue['id']`. So `gitlab:<n>` carries the iid. The mapping still persists both `gitlab_issue_id` and `gitlab_issue_iid`. github/jira unaffected.

#### issue_update(issue_id, title=None, description=None, status=None, labels=None)
- Updates existing issue fields
- All parameters optional - only provided fields updated
- Supports title, description, status, and labels

#### issue_sync(task_name)
- Syncs task status to linked provider issue
- Reads task status from frontmatter
- Updates provider issue state and adds comment

#### issue_push(task_name)
- Pushes task content changes to linked issue
- Updates issue description with current task content
- Preserves issue metadata

#### issue_link(task_name, issue_id)
- Links existing task to provider issue
- Validates issue ID format for provider
- Creates bidirectional mapping with metadata

#### issue_unlink(task_name)
- Removes issue link from task
- Cleans up provider-specific mapping
- Preserves task file, only removes link

#### issue_comment(issue_id, comment)
- Adds comment to provider issue
- Works with GitLab issues, Jira issues, GitHub issues

#### issue_set_status(issue_id, status, comment=None)
- Sets issue status/state directly
- Optional comment with status change
- Provider-specific status mapping

#### issue_api(method, endpoint, data=None)
- Direct provider API access for advanced operations
- method: GET, POST, PUT, DELETE
- Returns raw API response

#### issue_dependency(issue_number, depends_on_issue, remove=False, depends_on_repo=None, depends_on_owner=None)
- Manage issue dependencies — Gitea (full) and GitHub (same-repo only); not supported on GitLab
- Create a dependency, or remove it with `remove=True`; `depends_on_repo`/`depends_on_owner` are Gitea-only cross-repo targets
- Returns success status and dependency details

### Git/MR Tools (4 tools in tools/git_operations.py)

#### git_commit(message, add_all=True)
- Commits changes with message validation (empty + 1000-char length checks; **only a NUL byte is rejected** — multi-line messages with `Co-Authored-By:` trailers are supported, safe under `subprocess.run(shell=False)`; the former backtick/`$`/newline rejection was pointless under list-args and blocked trailers — m-enforcement-and-git-hardening)
- Optional staging of all changes (add_all=True default)
- Subprocess timeouts (l-fix-subprocess-timeout-hardening): `git add`/`git commit` carry `GIT_TIMEOUT_MEDIUM` (30s), imported from `engine_constants` via a `try/except ImportError` fallback to literals `15/30/60` (the hooks dir is on `sys.path` at server start, before the tool module imports, so the import normally succeeds; the fallback covers exotic standalone imports). A `subprocess.TimeoutExpired` returns a structured `{success:False, error:"Git commit timed out after 30s …"}` (dedicated handler ordered before `CalledProcessError`)

#### git_push(branch=None)
- Pushes to remote with upstream tracking
- Auto-detects current branch if not specified
- Branch name format validation: now **delegates to the shared hooks `git_operations.validate_branch_name`** (l-fix-security-hardening-residuals) via a module-level import-with-`ImportError`-fallback (mirroring the module's `engine_constants` timeout block) — same rule `^[a-zA-Z0-9/._-]+$` with dot allowed (dotted branches like `release/1.0.0` push) and explicit leading-dash rejection, applied before any git invocation (m-fix-mcp-git-review-tooling)
- Subprocess timeouts (l-fix-subprocess-timeout-hardening): `git branch --show-current` carries `GIT_TIMEOUT_FAST` (15s), `git push -u origin` carries `GIT_TIMEOUT_SLOW` (60s — the network op that can stall on a dead remote or a hung credential helper); a `subprocess.TimeoutExpired` returns a structured timeout error naming the 60s bound (the `code_review` MCP tool's own `git branch --show-current` likewise gained a 15s bound)

#### merge_request_create(task_name, source_branch, target_branch=None, labels=None)
- Creates GitLab merge request linked to task's issue
- `target_branch` defaults to the repo's **detected default branch** via the module-level `_detect_default_branch(project_root)`, which now delegates to the shared `git_operations.detect_default_branch` in hooks — the single source of truth also used by `OptimizeCompletionMixin._detect_default_branch` (origin/HEAD → main/master/develop/trunk/stable → "main"), not a hard-coded `"master"` (wrong on main-/develop-default repos — m-enforcement-and-git-hardening; single-sourced in l-refactor-code-quality-cleanup).
- Validates task-issue mapping exists
- Auto-generates MR title and description
- Label inheritance: explicit > issue+defaults > defaults only

#### merge_request_update(mr_iid, title=None, description=None, state_event=None)
- Updates existing GitLab merge request
- Supports title, description, state changes
- Input validation and size limits

### Protocol Tools (11 tools in tools/protocol.py)

#### protocol_list()
- List available protocols and their step sequences

#### protocol_start(protocol_name, task=None, resume_force_safe=False)
- Start a workflow protocol; sets DAIC mode and runs the first step's pre_funcs

#### protocol_current()
- Return the current protocol step, mode, and full step overview

#### protocol_advance(summary, args=None)
- Complete the current step (run post_funcs) and advance to the next

#### protocol_goto(step_name, reason)
- Jump back to a previous step to re-plan

#### protocol_log(task_name=None)
- Get the protocol audit log for a task (defaults to the current task)

#### protocol_abort(reason)
- Abort the active protocol and reset active-task indicators

#### protocol_save_note(note)
- Save a note to the protocol log (survives session restarts)

#### protocol_available_funcs()
- List the built-in and custom func handlers available to protocol steps

#### protocol_customize(protocol_name, force=False)
- Bootstrap-copy a system protocol's JSON + referenced sub-protocols + provider templates into `custom/` for full local editing
- Writes a provenance sidecar (`custom/.forked-from.json`) for later drift detection; no-clobber unless `force=True`
- Runs in the MCP server process (hook-exempt to read the protected `system/` tree); surfaced by `/team-management:custom-protocol-create`

#### protocol_check_drift(acknowledge=False)
- Detect/reconcile drift between forked custom protocols and the current system after a reinstall
- Stages each changed system file as `new-<basename>` next to its custom copy + returns a merge report
- `acknowledge=True` removes the staging and refreshes the sidecar hashes; surfaced by `/team-management:custom-protocol-update-after-reinstall`

### Code Review Tools (4 tools in tools/code_review.py)

#### merge_request_comment(mr_iid, comment)
- Adds comment to GitLab merge request
- Requires GitLab provider to be active
- Input validation (IID must be positive, comment length limits)

#### pull_request_comment(pr_number, comment)
- Adds comment to GitHub pull request
- Requires GitHub provider to be active
- Input validation (PR number must be positive)

#### code_review(branch=None, task_name=None, mr_iid=None, pr_number=None, review_text=None)
- Flexible automated code review workflow
- **Invocation patterns**:
  1. Task-based: `code_review(task_name="m-task")`
  2. MR-based: `code_review(mr_iid=123)`
  3. PR-based: `code_review(pr_number=456)`
  4. Branch-based: `code_review(branch="feature/x")`
  5. Auto: `code_review()` - uses current branch
  6. Post review: `code_review(mr_iid=123, review_text="...")`
- **Two-step workflow for external branches**:
  1. Call to fetch context
  2. Run code-review agent with returned context
  3. Call with review_text to post results

#### fetch_mr_review(mr_url_or_iid)
- Fetches existing code review from MR notes
- Accepts MR URL or IID
- Returns review text if found

### DAIC Management Tools (3 tools in tools/daic.py)

#### daic_mode_switch_discussion()
- Switch to discussion mode (blocks Edit/Write tools)
- No parameters - always switches to discussion
- Safe to auto-approve

#### daic_mode_switch_implementation()
- Switch to implementation mode (enables Edit/Write tools)
- No parameters - always switches to implementation
- May require user confirmation

#### daic_mode_switch_documentation()
- Switch to documentation mode (allows CLAUDE.md, task files, and docs/ edits; blocks source code)
- No parameters - always switches to documentation

### Notification Tools (3 tools in tools/notifications.py)

#### notify_user(message)
- Send a notification to the user through configured external channels (e.g. Telegram)

#### notification_status()
- Report whether user notifications are configured

#### notification_discover_telegram_chats(test_chat_id="")
- Config-flow helper for filling `notifications.channels.telegram.chat_id` without hand-copying it. Read-only/ungated; the bot token is read from the per-project token file and is NEVER passed as an argument.
- With `test_chat_id` empty: validates the token with `getMe`, then does a NON-mutating `getUpdates` read and returns the distinct `{id, type, title}` chats seen in RECENT updates (Telegram has no "list my groups" endpoint — a chat appears only if it recently messaged the bot or the bot was just added). A webhook makes `getUpdates` 409 (reported); no recent chats returns an actionable hint.
- With `test_chat_id` set: sends a one-line confirmation ping to that chat so the user can verify it before saving the chat id (Approach C, confirm-before-save).
- TLS verification for all Telegram calls goes through `notification_utils._telegram_ssl_context()` (certifi-aware; tolerates an empty python.org CA store — the failure the plugin's own MCP venv hit). `_telegram_api_call` returns `(payload, error, kind)` and classifies a TLS/transport failure distinctly from an HTTP auth rejection: a cert-verification failure returns a CA-fix `hint` (Install Certificates.command / `ca_bundle`), NEVER "token rejected". Optional `notifications.channels.telegram.ca_bundle` (or `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE`) overrides the CA bundle.

### Release Tools (1 tool in tools/release.py)

#### release_create(tag_name, release_name, description, asset_paths=None)
- Creates a release on GitLab and/or GitHub (both, if both are enabled)
- tag_name: Git tag for the release (e.g. "v1.0.0")
- release_name: Human-readable release name
- description: Release notes in markdown
- asset_paths: Optional list of absolute file paths to upload

### Config Tools (2 tools in tools/config.py — m-config-mcp-flow)

The config tools let `/team-management:config` read and write team-management's **non-secret** settings from inside Claude Code. Tokens are NEVER written here — they live in a per-project, user-authored `.claude/state/provider-tokens.json` (git-ignored, `0600`). `tools/config.py` is **self-contained** (does not import from the deleted `plugin/installer/`); the URL/IP validation it needs is implemented inline (SEC-007).

#### config_get()
- Read-only snapshot of `team-management/config.json` with sensitive values masked (tokens shown as `***set***` / `***unset***`, never echoed) via `_mask_sensitive`
- **Ungated** — safe to call any time to show current settings
- Returns `{success, exists, config, schema, config_path}`. The `schema` field (built by `_describe_schema()`) is the authoritative catalog of every setting `config_update` accepts — a list of `{key, type, allowed?, description}` for each `_CONFIG_SCHEMA` key, present on ALL return paths (exists / missing-file / unreadable). The `/team-management:config` flow reads it to learn a key's exact type/enum instead of guessing (secrets are never listed — they are rejected by `_SENSITIVE_KEY_RE`). Per-key one-line descriptions live in the parallel `_SCHEMA_DESCRIPTIONS` map, kept in sync with `_CONFIG_SCHEMA` by a bidirectional drift-guard test (m-fix-config-schema-exposure).

#### config_update(updates)
- Gated, schema-validated writer for NON-secret config keys; `updates` is a flat object of dotted keys (e.g. `{"developer_name": "Max", "issue_tracking.provider": "gitlab", "gitlab.base_url": "https://gitlab.com"}`)
- Pipeline, in order:
  1. **Intent-gate** — `shared_state.check_config_session_flag()` (TTL + existence; LLM cannot open the gate, only the deterministic `config_intent_gate.py` hook writes the flag). Refusal returns a `hint` to run `/team-management:config`.
  2. **Sensitive-key reject** — `_SENSITIVE_KEY_RE` (`token|secret|api[_-]?key|password|credential`) matched against the full dotted key BEFORE the schema check, so the error names the real reason (tokens → the per-project `.claude/state/provider-tokens.json`).
  3. **Schema validation** — `_validate_updates` against the `_CONFIG_SCHEMA` allowlist (unknown keys rejected; bool-before-int check since `bool` subclasses `int`; enum membership for `issue_tracking.provider`, `features.icon_style`, `notifications.mode`, `jira.api_version`). The allowlist covers the full non-secret settable surface — DAIC/workflow (`branch_enforcement.branch_prefixes`, `features.icon_style`), `code_review.enforce_warnings`, `notifications.*` (non-secret keys only; `bot_token` stays rejected), `github.default_labels`, and the `jira` scalars — so `config_update` no longer rejects a real `config.template.json` key as "unknown" (m-fix-config-schema-exposure).
  4. **SEC-007 URL validation** — `_validate_safe_url` for `_URL`-typed keys (`*.base_url`): https-only; rejects `localhost` / `*.local` / `*.localhost`, literal private/loopback/link-local/reserved/unspecified IPs (`ipaddress`), AND numeric-encoded IPs (bare-decimal / `0x` / `0o` / `0b` host forms — an SSRF validator-bypass class). Accepted residual gaps documented inline (DNS-resolved private host; dotted-octal `0177.0.0.1`).
  5. **git tracked-check** — refuses to write a git-tracked `config.json` (it may carry tokens into history); suggests `git rm --cached`.
  6. **gitignore-ensure** — `_ensure_gitignored` makes sure `config.json` is ignored by the host `.gitignore` (idempotent, best-effort, never blocks; degrades to `unavailable` on no-git-repo / `OSError` / non-UTF-8 `.gitignore`). Coverage is decided by `_gitignore_covers`, which recognizes the exact file entry AND any ancestor-directory entry (leading-`/` and trailing-`/` variants) — so a project that already ignores `team-management/` wholesale does NOT get a redundant `team-management/config.json` line appended (the m-harden-config-gitignore-guard fix). Lines are read with `rstrip` (git treats LEADING whitespace as significant but strips TRAILING); a later `!`-negation re-including the file/ancestor is treated as NOT covered so the positive entry is re-appended (last-match-wins re-ignore). Returns `{status: added|already_covered|unavailable, path, covered_by}`. Accepted gaps: exotic glob patterns, deeper dir-re-include negation, backslash-escaped trailing space.
  7. **read-modify-merge** — loads existing config, sets only the provided non-secret keys via `_set_dotted` (raises rather than clobbering a non-dict intermediate node); pre-existing token values are left untouched.
  8. **atomic write + 0600** — `shared_state._write_json_durable` then `chmod 0600` on POSIX (config lives next to tokens).
  8a. **core-config cache invalidation** (m-fix-mcp-config-cache-poisoning) — `core.config.reload_config()` (imported as `from core import config as core_config`, the canonical module identity — NOT `plugin.mcp.core.config`, which would be a separate module whose cache desyncs) immediately after the successful write + chmod, before the best-effort steps 8b–9. Without it the long-lived MCP server keeps serving the pre-write cached config (and a poisoned `{}` on a fresh install) until a Claude Code restart; `load_config()`'s mtime self-heal would eventually catch the write, but `detect_provider` caches `_provider` separately, so `reload_config()` (which nulls `_config`/`_provider` + clears the github/gitea singletons) is what makes a provider switch in this same call take effect. Steps 8b–9 read via `shared_state` (independent config cache), so clearing `core.config` here cannot corrupt them.
  8b. **task-template deploy** (h-fix-task-template-not-deployed) — `shared_state.ensure_task_template_deployed(project_root, shared_state.get_plugin_root())` after the successful write. `config_update` is what first creates `team-management/` on a fresh install, so it also deploys `team-management/tasks/TEMPLATE.md` (the file the protocols reference) WITHOUT waiting for the next session start — closing the same-session gap a session-start-only self-heal would leave. Best-effort and self-swallowing (create-if-absent atomic byte-copy); never affects the config-write result.
  8c. **guidance deploy+wire** (h-durable-guidance-via-claude-md) — `shared_state.ensure_guidance_deployed_and_wired(project_root, plugin_root, wiki_enabled)` after step 8b. Deploys the plugin-owned guidance files into the project and wires the project-root `CLAUDE.md` `@`-include managed block (deploy-before-wire), so `/team-management:config` and `/team-management:init` deliver durable behavioral guidance in-session — matching what `session-start.py` does on restart (the former SessionStart `additionalContext` injection faded over long / `/compact`-ed sessions). `wiki.enabled` is read from the merged config via an `isinstance`-guarded read so a non-dict `wiki` value degrades gracefully. Best-effort; never affects the config-write result.
  9. **TTL refresh** — `write_config_session_flag(preserve_session_id=True)` keeps the hook-written session id while extending the window.
- Returns `{success, updated_keys, config_path, gitignore}` on success — the `gitignore` field is the step-6 status dict `{status, path, covered_by}` so the `/team-management:config` flow can tell the user whether `config.json` was newly ignored (`added`), already covered, or could not be ignored (`unavailable`); structured `{success: False, error, errors?}` on any gate/validation failure
- Test: `test/test_config_mcp.py`

## Installation

### Server Layout
The modular MCP server lives under the plugin install at `${CLAUDE_PLUGIN_ROOT}/mcp/` (source: `plugin/mcp/`) and runs in place from PLUGIN_ROOT — there is no installer copy step into the project (the pip-era installer was retired in m-installer-retirement). Structure:

```
plugin/mcp/
    server.py              # Entry point
    core/
        __init__.py
        config.py
        project.py
    helpers/
        __init__.py
        code_review_utils.py
    tools/
        __init__.py
        issue_tracking.py
        protocol.py
        code_review.py
        git_operations.py
        daic.py
        notifications.py
        release.py
        config.py
```

### Provider Utility Strategy
**Zero-duplication architecture** - provider utilities are NOT copied:
- Server imports from `.claude/hooks/` at runtime
- Single source of truth for gitlab_utils.py, jira_utils.py, github_utils.py
- Automatic consistency between hooks and MCP tools
- No version skew between hook-based auto-sync and MCP operations

### Hook Registration
Hooks are registered declaratively by the plugin manifest `plugin/hooks/hooks.json`, which Claude Code reads automatically when the plugin loads — there is no installer writing or deduplicating hook entries in `.claude/settings.json` (the pip-era installer was retired in m-installer-retirement). The `normalize_command()` helper survives in `plugin/hooks/hook_utils.py` only so `boot_detector.py` can canonicalise legacy settings.json hook commands when probing for a coexisting pip-era install.

## Configuration

### MCP Server Registration (.mcp.json plugin manifest)
The MCP server is registered automatically through the plugin manifest `plugin/.mcp.json`; Claude Code reads it when the plugin loads, so there is no installer-written `.claude/settings.json` `mcpServers` block. The manifest points the `tm` server at the cold-start bootstrap (`python3 ${CLAUDE_PLUGIN_ROOT}/mcp/bootstrap_mcp.py`), which builds/validates the plugin venv before launching `server.py` (see the "Cold-Start Bootstrap" section above). Claude Code injects `CLAUDE_PLUGIN_ROOT`, `CLAUDE_PLUGIN_DATA`, and `CLAUDE_PROJECT_DIR` into the server process; the server resolves the project root from `CLAUDE_PROJECT_DIR` (env-first, then marker search).

**Provider tokens are read from a per-project file, NOT the OS keychain** (m-per-project-provider-tokens; supersedes the retired keychain / `userConfig` + on-disk-bridge model): every token-dependent MCP tool runs in the server (`issue_create`/`issue_read`/`issue_sync`/`issue_update` + the completion PR/MR via `resolve_provider_token`). Tokens now live in a **per-project, user-authored** `.claude/state/provider-tokens.json` — git-ignored, `0600`, a PROTECTED path no Claude tool (main-thread or subagent) can read — keyed by provider NAME. `resolve_provider_token` resolves **`file → config.json`** (config a legacy fallback; no env/keychain tier). The old OS-keychain `userConfig` model was retired because the keychain is global-per-plugin-per-user (two projects could not use different tokens); the intervening fixes it replaces — the 0.3.4 `.mcp.json` `env` `${...}` passthrough (never expanded by the host) and the 0.3.5 SessionStart-written keychain→`.claude/state/provider-tokens.json` bridge — are both gone. The seeder `shared_state.ensure_provider_tokens_file()` (session-start + `config_update`) create-if-absent seeds the template and never clobbers a user's file. **Accepted residuals:** a Bash-capable agent can read any user-readable file via a subprocess (the path-block is friction, not a barrier), and the codex/agy provider sandboxes have repo read access and can read the tokens file directly.

### Provider Configuration (config.json)
MCP server reads provider settings from standard configuration:
- `issue_tracking.provider` - Active provider selection
- `gitlab.*` - GitLab-specific settings
- `jira.*` - Jira-specific settings
- `github.*` - GitHub / Gitea-specific settings

## Label Management

### GitLab Label Support
The MCP server implements comprehensive label support for GitLab operations, ensuring consistency across issues and merge requests.

#### Label Inheritance Logic
The merge_request_create tool follows a three-tier priority system:

1. **Explicit Labels (Highest Priority)**
   - When labels parameter is provided explicitly
   - Overrides all automatic label inheritance
   - Use case: Custom label sets for specific workflows

2. **Issue + Default Labels (Medium Priority)**
   - Inherits labels from linked GitLab issue
   - Merges with configured default_labels from config.json
   - Deduplicates labels to prevent redundancy
   - Use case: Standard workflow with automatic label propagation

3. **Default Labels Only (Fallback Priority)**
   - Uses only default_labels from configuration
   - Applied when no issue link exists or explicit labels not provided
   - Use case: Standalone merge requests

#### Label Processing
- **Deduplication**: Prevents duplicate labels across sources
- **Format Handling**: Supports both string labels and dict labels (name field)
- **Consistency**: Maintains alignment between issue labels and MR labels
- **Configuration**: Default labels set via gitlab.default_labels in config.json

#### Related Functionality
- Issue creation: Also respects gitlab.default_labels setting
- Label consistency: Ensures issues and MRs share common label vocabulary
- System labels: Supports claude-code, automated, and custom project labels

## Integration Points

### Upstream Dependencies
- **Provider Utilities**: Imports from .claude/hooks/ (gitlab_utils, jira_utils)
- **Configuration**: Reads from team-management/config.json
- **State Files**: Accesses .claude/state/ for mappings and task state
- **FastMCP**: Requires the `mcp` package (official MCP SDK; provides `mcp.server.fastmcp.FastMCP`) for server implementation

### Downstream Consumers
- **Claude Code**: Registers MCP tools via .claude/settings.json or .mcp.json
- **Claude Agents**: Discover and invoke tools through MCP protocol
- **Task System**: Creates and updates task files in team-management/tasks/

### Related Systems
- **Hooks System**: Shares provider utilities and state files
- **Task Protocols**: Integrates with task lifecycle workflows

## MCP Tool Interface

All team-management functionality is exposed through `mcp__plugin_team-management_tm__*` tools. These are first-class tools integrated with Claude Code's tool system, providing:

- Automatic discovery in Claude's native tool list
- Structured error handling and type safety
- Direct tool calls without shell command expansion
- Consistent interface pattern with other Claude Code tools

## Performance Considerations

### Startup Time
- Minimal overhead from dynamic imports
- Configuration loaded lazily with caching
- No persistent connections or state
- Fast tool registration with FastMCP

### Runtime Performance
- Stateless operation model
- Configuration cache with mtime-based invalidation
- Efficient provider utility imports
- Direct API access without middleware

### Resource Usage
- No long-running background processes
- Minimal memory footprint
- Efficient file I/O for state access
- Network operations only when tools invoked

## Error Handling

### Provider Not Configured
- Returns structured error with setup instructions
- Provides provider-specific configuration guidance
- Does not raise exceptions that break tool execution
- Graceful degradation to manual setup

### Import Failures
- Clear error message if provider utils not found
- Guidance on installation location requirements
- Validates both installed and development paths
- Raises exception early if imports fail

### API Errors
- **Structured Error Returns**: Provider utilities return detailed error dictionaries from _make_request() including:
  - error_type: Classification (http_error, timeout, connection_error, request_error, unexpected_error)
  - status_code: HTTP status code for HTTP errors
  - message: Human-readable error description
  - response_body: First 500 characters of error response for debugging
  - url: API endpoint that was called
  - method: HTTP method used (GET, POST, PUT)
- **Exception Raising**: All API methods check for error responses and raise exceptions with full context
- **Error Message Format**: Exceptions include HTTP status, response body, and original error type
- **MCP Tool Integration**: MCP tools catch these exceptions and return structured error responses to Claude
- **Debugging Support**: Detailed error information aids troubleshooting without exposing sensitive tokens

## Security Considerations

### Input Validation
- Path traversal prevention in task names
- Command injection protection in git operations
- Size limits on text inputs (titles, descriptions)
- Format validation for branch names and issue IDs — branch names allow `[a-zA-Z0-9/._-]` and reject a leading dash (git option injection); MR/PR URL parsing is start-anchored so forwarded/embedded `github.com` paths cannot spoof the provider host (m-fix-mcp-git-review-tooling)

### Environment Security
- Project root detection via trusted environment variable
- No shell command execution from user input
- Token access through secure configuration only
- Restricted file system access scope

### State Protection
- Read-only access to most state files
- Controlled writes to mapping files only
- No modification of hook files or configuration
- Validation before any state changes

## Testing and Debugging

### MCP Server Testing
- Static tool-inventory drift-guard: `test/test_mcp_tool_inventory.py` (AST parse of `@mcp.tool` decorators; names/count/registration)
- Targeted runtime tests: `test/test_mcp_core.py` (provider detection, config load, disabled-provider `issue_status`, `get_project_root` reconciliation, a DAIC tool round-trip, and a **protocol-tools end-to-end round-trip** — m-test-hook-and-mcp-coverage-gaps: `protocol_start`/`protocol_current`/`protocol_advance` driven through the `MockMCP.get_tool()` harness against a temp project via a minimal 2-step custom protocol; the `protocol_project` fixture patches the frozen `shared_state` path constants + resets the `core.*` caches, and the round-trip byte-compares the live `.claude/state/current_task.json` to prove it is untouched) — pytest-native, tmp `CLAUDE_PROJECT_DIR`, `core.*` module identity, never touches the live `.claude/state`. Replaced the retired `plugin/mcp/test_server.py`, whose `(env, results)` signatures polluted every `pytest` run and mutated live DAIC state (m-statusline-and-test-infra).
- Provider routing and selection testing
- Configuration cache invalidation testing
- `test/test_mcp_git_review_tooling.py` — 19 regression tests (MockMCP + `patch.object` pattern) covering dotted-branch/leading-dash validation in `validate_branch_name` and `git_push`, anchored GitHub URL parsing in `parse_mr_pr_url`, exact-ref stash pop + timeout handling + success formula in `restore_git_environment`, and the `detect_provider` cache-invalidation order (m-fix-mcp-git-review-tooling)

### Debug Support
- Detailed error messages with context
- Provider detection logging
- Import path resolution debugging
- Configuration validation feedback

### Development Mode
- Supports running from development directory
- Fallback to `plugin/hooks/` for imports (via `get_hooks_path()` `__file__`-relative + legacy candidates) when neither `CLAUDE_PLUGIN_ROOT` nor `.claude/hooks/` resolves
- No installation required for testing
- Hot reload of provider utility changes

## Related Documentation

- `.claude/hooks/CLAUDE.md` - Provider utilities documentation (single source of truth)
- `docs/MCP_SERVER.md` - MCP server architecture and usage guide
- `team-management/config.json` - Configuration examples with provider settings
