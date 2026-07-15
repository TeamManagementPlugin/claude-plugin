---
title: MCP Server
tags: [mcp, architecture]
created: 2026-05-31
updated: 2026-07-13
sources: [plugin/mcp/server.py, plugin/requirements.in, plugin/.mcp.json, plugin/hooks/shared_state.py, plugin/hooks/session-start.py, plugin/hooks/sessions-enforce.py, plugin/mcp/core/config.py, plugin/mcp/tools/config.py, plugin/mcp/core/project.py, plugin/mcp/helpers/code_review_utils.py, plugin/mcp/tools/protocol.py, plugin/mcp/tools/issue_tracking.py, plugin/mcp/tools/git_operations.py, plugin/mcp/tools/code_review.py, plugin/mcp/tools/release.py, plugin/mcp/tools/notifications.py, plugin/hooks/notification_utils.py, plugin/hooks/gitlab_utils.py, plugin/hooks/github_utils.py, test/test_mcp_manager_split.py]
---

# MCP Server

The MCP server is the **primary tool surface** of team-management: a FastMCP-based process that exposes issue tracking, protocol engine, code review, git, DAIC mode switching, notifications and release operations as native `mcp__plugin_team-management_tm__*` tools in Claude Code. It exists so that workflow operations are first-class typed tools with structured error handling and guaranteed discoverability, rather than shell commands Claude must remember to format. It is stateless: every tool delegates to the same provider utilities and protocol engine the hooks use (resolved plugin-root-first from `${CLAUDE_PLUGIN_ROOT}/hooks`), so the MCP server and the hooks always run identical underlying code (see [Issue Tracking Providers](pages/subsystems/issue-tracking-providers.md), [Protocol Engine](pages/subsystems/protocol-engine.md)).

## Entry point and bootstrap

`server.py` is intentionally thin (~95 lines, mostly a docstring tool catalog). The mechanics:

1. **sys.path bootstrap** (`server.py`): inserts its own directory onto `sys.path` *before* any package import. This is required because Claude Code launches `server.py` as a **script**, not as a package module, so relative imports like `from .core.project import ...` would raise `ImportError`. Every internal import is therefore absolute (`from core.project import ...`, `from tools import daic`).
2. **Provider import setup** (`server.py`): calls `setup_provider_imports()` once at startup to wire `.claude/hooks/` onto `sys.path` (see below).
3. **FastMCP instance** (`server.py`): `mcp = FastMCP("team-management")`. `FastMCP` is imported from `mcp.server.fastmcp` — the FastMCP bundled with the official **`mcp` SDK** (declared in `plugin/requirements.in` as `mcp>=1.28.0`), **not** the standalone `fastmcp` PyPI package. The standalone `fastmcp` is unused; it was dropped from `requirements.in` (l-verify-mcp-server-fastmcp-import) so the import is backed by a directly-declared dependency rather than a transitive one, and to shed its heavyweight closure (the lock slimmed 79→36 pins).
4. **Module registration** (`server.py`): imports the 8 tool modules and calls `<module>.register_tools(mcp)` on each.

`main()` (`server.py`) is the console entry point; it just calls `mcp.run()`.

A prominent comment block (`server.py`) documents that a `workflow_toggle` tool was **deliberately removed** — Claude must not be able to disable its own DAIC enforcement. Disabling enforcement is a user-only terminal operation (`workflow_command.py`). See [DAIC Enforcement](pages/topics/daic-enforcement.md).

## The `register_tools(mcp)` pattern

Each module under `tools/` exports exactly one function, `register_tools(mcp)`, that defines the module's tools as nested functions decorated with `@mcp.tool()`. The decorator closes over the `mcp` instance passed in, so there is no module-level server singleton. This keeps each tool category independently testable and lets `server.py` compose the full tool set by calling the registrars in sequence.

The 8 modules and their tool counts (42 total, verified by counting `@mcp.tool` decorators):

| Module | Tools | Examples |
|---|---|---|
| `tools/issue_tracking.py` | 14 | `issue_status`, `issue_read`, `issue_create`, `issue_sync`, `issue_link`, `issue_dependency`, plus `config_issue_tracking_status`, `config_code_review_enforcement` |
| `tools/config.py` | 2 | `config_get`, `config_update` |
| `tools/protocol.py` | 11 | `protocol_list`, `protocol_start`, `protocol_advance`, `protocol_goto`, `protocol_customize`, `protocol_check_drift` |
| `tools/code_review.py` | 4 | `code_review`, `fetch_mr_review`, `merge_request_comment`, `pull_request_comment` |
| `tools/git_operations.py` | 4 | `git_commit`, `git_push`, `merge_request_create`, `merge_request_update` |
| `tools/daic.py` | 3 | `daic_mode_switch_discussion` / `_implementation` / `_documentation` |
| `tools/notifications.py` | 3 | `notify_user`, `notification_status`, `notification_discover_telegram_chats` |
| `tools/release.py` | 1 | `release_create` |

A static drift-guard test (`test/test_mcp_tool_inventory.py`) parses these decorators with `ast` (no `mcp` import) and asserts the set/count match a canonical `EXPECTED_TOOLS` and the doc header — adding/renaming a tool without updating docs fails CI.

## Single-source-of-truth imports (the core design decision)

The MCP server never copies provider code. Instead it imports `gitlab_utils`, `jira_utils`, `github_utils`, `issue_provider_base`, `shared_state`, and `protocol_engine` from the plugin's hooks dir (resolved by `get_hooks_path()`, plugin-root-first).

- `get_hooks_path()` (`core/project.py`) resolves the hooks directory: it tries `${CLAUDE_PLUGIN_ROOT}/hooks` first (when set — the plugin install), then the deployed `<root>/.claude/hooks`, then the dev `plugin/hooks` location, gating each on the existence of `gitlab_utils.py`. Raises a descriptive exception if none exists.
- `setup_provider_imports()` (`core/project.py`) prepends that path to `sys.path` (idempotent — only if not already present). It must be called before any provider import; `config.py` and `code_review_utils.py` call it defensively at each call site.
- `_import_from_hooks(module_name)` (`core/project.py`) is the modern idiom: `setup_provider_imports()` then `importlib.import_module(name)`, returning the module object. `tools/protocol.py` uses it to obtain `shared_state` and `protocol_engine` (`tools/protocol.py`).

**Why this matters:** importing from a single canonical location guarantees hook-driven auto-sync and MCP tool operations execute byte-identical GitLab/Jira/GitHub API code — no version skew between the two paths.

## Project-root detection (environment-based)

`get_project_root()` (`core/project.py`) resolves the root with a cached, ordered strategy:

1. **`CLAUDE_PROJECT_DIR` env var** (set by Claude Code) — trusted first, if the path exists.
2. **Marker search** upward from `cwd` for any of `.git`, `team-management`, `sessions`, `pyproject.toml`.
3. **Fallback** to `cwd`.

The result is cached in module-global `_project_root`. This env-var-first approach is deliberate: the MCP server may be launched with an arbitrary working directory, so the harness-provided `CLAUDE_PROJECT_DIR` is the authoritative signal (Claude Code sets it for the plugin's hooks and MCP server).

## Provider routing and config caching

`core/config.py` turns config into a live provider instance:

- `load_config()` (`config.py`) reads `team-management/config.json` with **existence-and-mtime cache invalidation** — one guarded `stat()` yields the file's mtime, or the `-1.0` sentinel when the file is missing/unreadable (distinguishable from any real mtime ≥ 0), and it calls `reload_config()` whenever that value changed since last load. Because every cache state stamps `_config_mtime` (`-1.0` when absent, the file's own mtime on success **and** on the malformed-file exception path), a `config.json` that appears after a first file-absent load — the fresh-install order where a provider tool touches config before `/team-management:config` writes it — or one later deleted is picked up on the next call, not cached for the process lifetime. On any read error it silently returns `{}` to avoid corrupting the STDIO MCP protocol with stack traces (m-fix-mcp-config-cache-poisoning; before, the missing-file and exception paths left `_config_mtime = None`, so once `{}` was cached before the file existed it was returned until an MCP server restart).
- `detect_provider()` (`config.py`) calls `load_config()` **before** consulting its `_provider` cache, so the mtime-based invalidation (which clears `_provider` via `reload_config`) actually runs — a change to `issue_tracking.provider` takes effect without an MCP server restart (m-fix-mcp-git-review-tooling; previously the cache check ran first and pinned the provider for the process lifetime). It reads `issue_tracking.provider`; if not `"disabled"` it validates that the named provider's section has `enabled: true`. It then **falls back** to scanning individual `gitlab/jira/github` `enabled` flags. Returns `None` when nothing is enabled. Result cached in `_provider`.
- `get_provider_api()` / `get_provider_sync()` (`config.py`) call `setup_provider_imports()`, then return the matching class: `GitLabAPI`/`GitLabTaskSync`, `JiraProvider`/`JiraTaskSync`, or — for GitHub/Gitea — the **singletons** `get_github_api()` / `get_github_sync()`. The singleton is load-bearing for Gitea: Gitea requires numeric label IDs (not names), so a cached instance avoids redundant label-lookup/creation API calls across multiple tool invocations.
- `reload_config()` (`config.py`) clears all three caches and additionally calls `github_utils._clear_singletons()` (guarded — older versions lack it) so fresh config produces fresh provider instances. The `config_update` tool (`tools/config.py`) calls it (via the canonical `from core import config as core_config`) immediately after its successful durable write, so a config change made through `/team-management:config` takes effect in the same long-lived server process — including a provider switch, since `_provider` is a separate cache that `load_config()`'s mtime self-heal alone would not clear (m-fix-mcp-config-cache-poisoning).

## Code-review helper environment management

`helpers/code_review_utils.py` carries the non-trivial git choreography behind the `code_review` tool:

- `parse_mr_pr_url()` (`code_review_utils.py`) distinguishes GitLab MR URLs (`/-/merge_requests/N`) from GitHub PR URLs (`/pull/N`, including Enterprise `…/api/v3`). All patterns are **start-anchored** (`^https?://`, `re.match`) so a forwarded path like `https://evil.com/forward/github.com/owner/repo/pull/123` cannot spoof the provider host; scheme-less URLs intentionally do not parse — provider `web_url`/`html_url` fields always carry a scheme (m-fix-mcp-git-review-tooling). Explicit **ReDoS hardening** kept: rejects URLs with >20 slashes and caps GitLab path segments at 10 in the regex.
- `validate_branch_name()` restricts branch names to `[a-zA-Z0-9/._-]+` (dot included so `release/1.0.0` validates) and rejects a leading `-` outright — a leading dash would be parsed as a git option (option injection). The same rule is duplicated inline in `git_push` (`tools/git_operations.py`) (m-fix-mcp-git-review-tooling).
- `prepare_review_environment()` / `restore_git_environment()` implement a stash-and-checkout round trip: save current branch → detect uncommitted changes via `git status --porcelain` → stash with a fixed sentinel message `"Temporary stash for code review"` → fetch/checkout/pull the target branch; restore reverses it. Restore pops the **exact `stash@{N}` ref** parsed from the `git stash list` line matching the sentinel message (a bare `git stash pop` would pop an interloper stash pushed by another process between prepare and restore); all three restore git calls carry timeouts (checkout 10s, stash list 10s, stash pop 30s) with an explicit `TimeoutExpired` catch carrying a manual-resolution message; and `success` is `restored and (not was_stashed or unstashed)` — a failed or missing stash pop is reported as failure, not silently dropped (m-fix-mcp-git-review-tooling). All subprocess calls use `stdin=subprocess.DEVNULL`.

## Gotchas

- **Script vs module imports.** The sys.path bootstrap (`server.py`) is mandatory. Any new internal import must be absolute (`from core.x`, `from tools.y`), never relative — relative imports break under MCP's script launch.
- **Inline count comments can drift.** Trust the `@mcp.tool` decorator count and the `test/test_mcp_tool_inventory.py` inventory test over any inline tool-count comment in `server.py`.
- **`CLAUDE_PROJECT_DIR` must be `${workspaceFolder}`.** In the MCP config the env var must use `${workspaceFolder}`, not `$CLAUDE_PROJECT_DIR` — the latter expands in the wrong context and root detection silently falls back to `cwd`.
- **Silent empty config.** `load_config()` returns `{}` on *any* exception (`config.py`), including malformed JSON. Provider detection then reports "no provider" rather than surfacing the parse error — a fat-fingered config looks like a disabled provider, not a crash.
- **Caches persist for the process lifetime.** `_project_root`, `_config`, and `_provider` are module globals. `_config`/`_provider` are auto-invalidated on a config **existence-or-mtime** change (and actively by `config_update`), so a create/delete/edit of `config.json` is picked up without a restart — but `_project_root` is never invalidated (fine for a single-root server, a trap for any test that reuses the module across roots). Tests that exercise the config cache must reset `_config`/`_config_mtime`/`_provider` too, not just `_project_root` — importing the module as `core.config` (NOT `plugin.mcp.core.config`) keeps the test and the server on the same module identity so those caches stay in sync.
- **`git_commit` is intentionally non-autonomous.** Its docstring (`git_operations.py`) instructs Claude to call it *only* on explicit user request, never as part of a protocol — protocol-driven commits go through the engine's completion funcs (see [Completion and Git Flow](pages/procedures/completion-and-git-flow.md)). It also rejects backticks, `$`, and newlines in the message.
- **`customize_protocol` must run in the MCP process.** It reads the protected `team-management/protocol-configs/system/` tree, which is hook-blocked for ordinary agent tools — only the MCP server is exempt. See [Protocol Engine](pages/subsystems/protocol-engine.md).
- **MR/PR/release methods live on manager classes, not the bare API.** After the provider layer was split, `add_mr_comment` / `get_merge_request` / `find_merge_request_by_branch` / `get_mr_notes` / `update_merge_request` / `create_merge_request` / `create_release` (GitLab MR + release) and `find_pull_request_by_branch` / `create_release` (GitHub PR + release) moved OFF `GitLabAPI`/`GitHubAPI` and onto `GitLabMRManager` / `GitLabReleaseManager` / `GitHubPRManager` / `GitHubReleaseManager` (re-exported from `gitlab_utils`/`github_utils`). The `code_review.py` / `git_operations.py` / `release.py` tools and `helpers/code_review_utils.py` must construct the manager wrapping the API — `GitLabMRManager(gitlab).get_merge_request(...)`. Calling a moved method on a bare `GitLabAPI()`/`GitHubAPI()` raises `AttributeError`, which the outer `except Exception` swallows into a generic failure, so the tool looks implemented but silently never works. This bit 16 call sites (found in h-fix-mcp-manager-split-and-notebookedit; guarded by `test/test_mcp_manager_split.py`, which drives each tool fn → real manager with mocked HTTP). Note the exceptions: `GitHubAPI.add_comment` (PR comments) and `GitHubAPI._make_request` did NOT move — they stay on the API.
- **`detect_provider` double-checks `enabled`.** Setting `issue_tracking.provider: "gitlab"` without `gitlab.enabled: true` does not select GitLab via the primary path; it then falls through to the individual-flag scan. Configure both. See [Configuration Schema](pages/entities/configuration-schema.md).
- **Provider tokens live in a per-project, user-authored file.** `.claude/state/provider-tokens.json` (git-ignored, `0600`, keyed by provider NAME — `gitlab`/`jira`/`github`/`telegram`) holds the tokens; the user creates and fills it. The OS-keychain `userConfig` model was retired in `m-per-project-provider-tokens` because the keychain is global-per-plugin-per-user, so two projects could not use different tokens (`plugin.json` no longer declares `userConfig`). Every token-dependent tool (`issue_*`, completion PR/MR) runs in the server via `shared_state.resolve_provider_token`, which resolves **file → `config.json`** — `config.json` is a legacy fallback for existing installs; there is NO env/keychain tier. The file is seeded create-if-absent by `ensure_provider_tokens_file` (a template with all four keys blank + an explanatory `_comment`; `0600`; NEVER overwrites/deletes an existing file, and re-applies `chmod 0600` to a pre-existing user-created file via `_chmod_0600`), called from the SessionStart hook and the `config_update` MCP tool; `.claude/` gitignoring is ensured via `ensure_claude_dir_gitignored` (moved into `shared_state.py`). Legacy `CLAUDE_PLUGIN_OPTION_*` env-name keys written by older bridge files are still READ as a back-compat fallback. That file is a PROTECTED path — no Claude tool, main-thread or subagent, can read it (`sessions-enforce` `PROTECTED_PATHS`, with `_collapse_redundant_segments` closing `/./` and `//` spelling bypasses); a dedicated `_targets_token_bridge` guard ahead of the workflow-bypass exit keeps direct access blocked even when bypass disables the rest of enforcement. Symptom when broken: completion reported `Provider 'github' MR not implemented` (now an honest `skipped` + token reason). **Accepted residuals:** a Bash-capable agent can read any user-readable file via a subprocess (the path-block is friction, not a hard barrier), and the codex/agy provider sandboxes have repo read access and can read the file directly.
