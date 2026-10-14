# MCP Module CLAUDE.md

## Purpose
FastMCP server exposing team-management as MCP tools: protocol engine, DAIC switching, issue tracking, git/MR operations, code review, notifications, releases and configuration. Tools are namespaced `mcp__plugin_team-management_tm__<tool>` (server key `tm` in `plugin/.mcp.json`).

## Startup
- `plugin/.mcp.json` runs `python3 ${CLAUDE_PLUGIN_ROOT}/mcp/bootstrap_mcp.py`.
- `bootstrap_mcp.py` runs under the system python before the venv exists, so it is pure stdlib and must never import `server.py`. It:
  - builds `${CLAUDE_PLUGIN_DATA}/venv` when it is missing or the recorded sha256 of `requirements.lock` differs (`python -m venv` + `pip install --require-hashes`);
  - writes the hash file LAST, so a crashed build self-heals on the next start;
  - serializes builds behind an interprocess lock with a double-checked `needs_build()`, and prints a breadcrumb only when the lock is contended;
  - bounds each build step (`BUILD_STEP_TIMEOUT = 600`) and sends captured output to stderr — stdout belongs to the MCP protocol;
  - exits with a repair message when a build is needed but `requirements.lock` is missing;
  - hands off to the venv python running `server.py` via `os.execv` (POSIX) or `subprocess.run` (Windows), before any MCP handshake.
- On Windows the venv's `python.exe` is a copy, so a system-Python upgrade does not trigger a rebuild. Delete the venv to force one.
- `server.py` runs as a script: it adds its own directory to `sys.path` and uses absolute imports (`from core.project import ...`). Relative imports fail there.
- Keep the literal `python3` token in the manifest; Windows gets a `python3.exe` from `/team-management:init`.

## Layout
```
plugin/mcp/
    bootstrap_mcp.py       # cold-start launcher (stdlib only)
    server.py              # FastMCP("team-management"); calls each module's register_tools(mcp)
    core/
        config.py          # load_config (existence-or-mtime cache), detect_provider, provider API/sync factories
        project.py         # get_project_root, get_hooks_path, _import_from_hooks, task file lookup
    helpers/
        code_review_utils.py # MR/PR URL parsing, review environment prep/restore
    tools/
        issue_tracking.py  # 14 issue tracking tools
        protocol.py        # 11 protocol engine tools
        code_review.py     # 4 code review tools
        git_operations.py  # 4 git/MR tools
        daic.py            # 3 DAIC mode switching tools
        notifications.py   # 3 notification tools
        release.py         # 1 release creation tool
        config.py          # 2 config tools
```

## Core Behaviour
- Provider code is imported, never copied. `get_hooks_path()` returns the first existing dir containing `gitlab_utils.py`, searched in this order: `${CLAUDE_PLUGIN_ROOT}/hooks` → `<project>/.claude/hooks` → dev `plugin/hooks` (via `__file__`) → `<project>/plugin/hooks`.
- Hooks and MCP tools therefore run byte-identical provider code.
- `_import_from_hooks(name)` puts the hooks dir on `sys.path` and imports a module from it. Use it instead of ad-hoc path edits.
- `core/project.get_project_root()` reads `CLAUDE_PROJECT_DIR` first. It cannot delegate to `shared_state` (this module bootstraps the path that makes `shared_state` importable). Equality with `shared_state` is asserted by `test/test_mcp_core.py`.
- `core/config.load_config()` re-checks file existence and mtime on every call with one guarded `stat()` (`-1.0` = missing):
  - a config created or deleted after the first load is picked up;
  - a malformed file returns `{}` and is not re-parsed until its mtime changes.
- `detect_provider()` reads config before checking its cache, so a changed `issue_tracking.provider` applies without a server restart. GitHub/Gitea API objects are singletons (label cache).
- MR/PR/release calls go through the manager classes (`GitLabMRManager`, `GitHubPRManager`, release managers). The `*_utils.py` facades re-export them.
- `helpers/code_review_utils.py`:
  - `parse_mr_pr_url` anchors on `^https?://`;
  - `validate_branch_name` delegates to the hooks' `git_operations.validate_branch_name`;
  - every git call in review prep/restore has a timeout;
  - restore pops the exact `stash@{N}` it created.
- Errors are returned as structured `{success: False, error, ...}` dicts. Nothing may print to stdout (it is the STDIO transport).

## MCP Tools (42 total)

### Issue tracking (`tools/issue_tracking.py`, 14)
- `issue_status` — provider configuration and linked tasks.
- `config_issue_tracking_status` — config summary for protocol decisions.
- `config_code_review_enforcement(task_content)` — `enforce_warnings` mode, warning analysis of the task's review block, and guidance text from `protocol_utils.get_review_completion_language`.
- `issue_read(issue_id_or_url, update_mode=False)` — import an issue as a task (or refresh one).
- `issue_create(task_name)` — create an issue from a task.
- `issue_update` / `issue_sync` / `issue_push` — update an issue, sync task status, push task content.
- `issue_link` / `issue_unlink` — manage task ↔ issue mappings.
- `issue_comment` / `issue_set_status` / `issue_api` — comment, set status, raw provider API.
- `issue_dependency` — issue dependencies (Gitea, GitHub same-repo).

### Git / MR (`tools/git_operations.py`, 4)
- `git_commit(message, add_all=True)`, `git_push(branch=None)`, `merge_request_create(...)`, `merge_request_update(...)`.

### Protocol (`tools/protocol.py`, 11)
- `protocol_list`, `protocol_start(protocol_name, task=None, resume_force_safe=False)`, `protocol_current`, `protocol_advance(summary, args)`, `protocol_goto(step_name, reason)`, `protocol_log`, `protocol_abort(reason)`, `protocol_save_note(note)`, `protocol_available_funcs`.
- `protocol_customize(protocol_name, force=False)` / `protocol_check_drift(acknowledge=False)` — fork a system protocol into `custom/` and reconcile after an update. They read the protected system tree, so they must run in the MCP server process.
- `protocol_save_note` stamps a working-tree hash on `SPEC_REVIEW: PASSED`, `SPEC_DEFERRED: PASSED` and `REVIEW_ROUND:` notes (see `plugin/hooks/CLAUDE.md`).

### Code review (`tools/code_review.py`, 4)
- `merge_request_comment`, `pull_request_comment`, `code_review(...)` (automated review workflow), `fetch_mr_review(mr_url_or_iid)`.

### DAIC (`tools/daic.py`, 3)
- `daic_mode_switch_discussion`, `daic_mode_switch_implementation`, `daic_mode_switch_documentation` — manual override. Protocols set the mode per step.

### Notifications (`tools/notifications.py`, 3)
- `notify_user(message)`, `notification_status`, `notification_discover_telegram_chats(test_chat_id="")`.

### Release (`tools/release.py`, 1)
- `release_create(tag_name, release_name, description, asset_paths=None)`.

### Config (`tools/config.py`, 2)
- `config_get` — ungated, masked snapshot of `team-management/config.json` (tokens shown as `***set***` / `***unset***`). Every return path includes a `schema` catalog (`{key, type, allowed?, description}`) built from `_CONFIG_SCHEMA`.
- `config_update(updates)` — writes flat dotted non-secret keys. Pipeline, in order:
  1. intent gate — `check_config_session_flag`; only the `config_intent_gate.py` hook can open it;
  2. sensitive-key reject (`token|secret|api key|password|credential`) — tokens belong in `.claude/state/provider-tokens.json`;
  3. schema validation against `_CONFIG_SCHEMA` (bool checked before int; enums);
  4. SEC-007 URL check for `*.base_url` — https only, no localhost / private / numeric-encoded IPs;
  5. refuse a git-tracked `config.json`;
  6. ensure `config.json` is gitignored;
  7. read-modify-merge;
  8. atomic write + `chmod 0600`;
  9. `core.config.reload_config()` — import it as `from core import config`; a second module identity would keep a stale cache;
  10. deploy the task template;
  11. deploy and wire the guidance;
  12. deploy the agy gate and write the agy permission rules (consent-only, when agy is enabled);
  13. refresh the session TTL.
- `config_update` returns `{success, updated_keys, config_path, gitignore, agy_permission_rules?}`.
- Every `_CONFIG_SCHEMA` key needs a `_SCHEMA_DESCRIPTIONS` entry (`test/test_config_mcp.py`).

## Drift Guards
- `test/test_mcp_tool_inventory.py` parses the `@mcp.tool` decorators with `ast` and asserts the tool set, the per-module counts, and the `## MCP Tools (N total)` header above. Update all three together.
- `test/test_mcp_tool_namespace.py` forbids any tool-name form other than `mcp__plugin_team-management_tm__<tool>` under `plugin/`.
- Bootstrap: `test/test_bootstrap_mcp.py`. Config tools: `test/test_config_mcp.py`.
