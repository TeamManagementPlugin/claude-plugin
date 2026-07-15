---
title: Configuration Schema
tags: [config, installer, ai-providers]
created: 2026-05-31
updated: 2026-07-12
sources: [plugin/templates/config.template.json, plugin/mcp/core/config.py, plugin/mcp/tools/config.py, plugin/hooks/protocol_engine.py, plugin/hooks/session-start.py, plugin/hooks/shared_state.py, plugin/hooks/notification_utils.py, plugin/mcp/tools/notifications.py, plugin/commands/config.md, plugin/templates/statusline.py]
---

# Configuration Schema

`team-management/config.json` is the single runtime configuration file for a project. It is seeded from the template `plugin/templates/config.template.json` by the config flow (`/team-management:config` offers to create it when absent); non-secret keys are then written through the `config_update` MCP tool, while secret provider tokens live in the per-project `.claude/state/provider-tokens.json` file (git-ignored, `0600`, a PROTECTED path the AI cannot read) — the OS-keychain `userConfig` model was retired in `m-per-project-provider-tokens`. Every hook, the protocol engine, and the MCP server read it back at runtime. This page is a reference catalog of its keys, grouped by area, with the code that consumes each key. For where it sits relative to the rest of the system, see [Architecture Overview](pages/overview/architecture-overview.md).

The file is plain JSON with no schema validation at write time. Consumers read it defensively: most use `config.get("section", {}).get("key", default)` so a missing key falls back to a hardcoded default, and a malformed or unreadable file degrades gracefully rather than crashing the workflow (see Gotchas).

## How it is loaded

The MCP server caches the parsed config with mtime-based invalidation (`plugin/mcp/core/config.py`): `load_config()` re-reads from disk only when `config.json`'s `st_mtime` changes, otherwise returns the cached dict. A parse failure returns `{}` silently (`config.py`) so a broken config never breaks the STDIO MCP protocol. `reload_config()` clears the cache and provider singletons. Hooks generally re-open the file per invocation rather than caching.

The template ships extensive `_comment_*` keys (e.g. `_comment_ai_providers`, `_comment_test_command`, `_comment_branch_prefixes`). These are inert documentation — JSON has no comment syntax, so they are read as ordinary keys that no consumer looks for.

## Key catalog

### Top-level / identity
- `developer_name` (`config.template.json`) — how Claude addresses the user.
- `project_name` (`config.template.json`) — optional statusline label rendered on line 2, between the open-tasks and MCP segments (`plugin/templates/statusline.py`). Read via `shared_state.load_config()` (kept only when a non-empty post-strip string) and settable through `config_update` (`plugin/mcp/tools/config.py`). Empty / unset / whitespace-only / non-string falls back to the project folder name (`PROJECT_ROOT.name`).
- `api_mode` — coarse workflow toggle.
- `features.icon_style` — `nerd_fonts` / `emoji` / `ascii`; statusline glyph set (also selects the `project_name` segment's icon). Default `ascii` on Windows, `nerd_fonts` elsewhere.

### Protocol engine and DAIC
- `protocol_engine.enabled` — master switch for the JSON-driven protocol engine. See [Protocol Engine](pages/subsystems/protocol-engine.md).
- `blocked_tools` — tools blocked in discussion mode (`Edit`, `Write`, `MultiEdit`, `NotebookEdit`). Consumed by the DAIC enforcement hook; see [DAIC Enforcement](pages/topics/daic-enforcement.md).
- `task_detection.enabled` — enable/disable task-based workflows.

### Branch enforcement
- `branch_enforcement.enabled` — git branch checking on/off.
- `branch_enforcement.branch_prefixes` — task-prefix → branch-prefix map: `implement-`→`feature/`, `fix-`→`fix/`, `o-`→`optimize/`, `b-`→`brainstorm/`, etc. The `o-`→`optimize/` and `b-`→`brainstorm/` mappings are required by the optimize and brainstorm protocols respectively. If the whole `branch_prefixes` block is omitted, the engine falls back to `DEFAULT_CONFIG.branch_prefixes` baked into `sessions-enforce.py`.

### Issue tracking
- `issue_tracking.provider` — active provider: `"gitlab"`, `"jira"`, `"github"`, or `"disabled"`. This is the primary selector read by `detect_provider()` (`config.py`).
- `issue_tracking.auto_sync` — global auto-sync toggle.

`detect_provider()` resolution order (`config.py`): read `issue_tracking.provider`; if it is not `"disabled"`, confirm the matching provider block has `enabled: true` and use it; otherwise **fall back** to scanning `gitlab.enabled` → `jira.enabled` → `github.enabled` in that fixed order. The fallback means a legacy config with no `issue_tracking.provider` key but `gitlab.enabled: true` still resolves to GitLab. `"disabled"` is also what routes completion through the 4-option local menu — see [Completion and Git Flow](pages/procedures/completion-and-git-flow.md).

Provider blocks (each gated by its own `enabled` flag; all default `false` in the template). See [Issue Tracking Providers](pages/subsystems/issue-tracking-providers.md) for the API mechanics.
- `gitlab`: `enabled`, `api_token`, `base_url` (default `https://gitlab.com`), `project_path` (`namespace/project`), `auto_sync`, `default_labels`.
- `jira`: `enabled`, `api_version` (`"2"`), `api_token`, `base_url`, `project_key`, `auto_sync`, `default_issue_type` (`"Task"`), `supported_issue_types`.
- `github`: `enabled`, `api_token`, `base_url` (default `https://api.github.com`; Gitea uses `https://<host>/api/v1`, supplied by the user in the config flow and detected at runtime via `github/api.py::is_gitea`), `repository` (`owner/repo`), `auto_sync`, `default_labels`, `workflow_labels` (state→label map: `in_progress`/`blocked`/`pending`).

`get_provider_api()` / `get_provider_sync()` (`config.py`) dispatch on the detected provider to the right wrapper class. GitHub/Gitea uses a singleton (`get_github_api`/`get_github_sync`) to preserve a label-ID cache across MCP calls; GitLab and Jira instantiate fresh.

### AI providers
- `ai_providers.enabled_providers` — list of active providers, e.g. `["codex", "agy"]`. Empty by default.
- Six per-phase flags, each `true` in the template: `include_in_code_review`, `include_in_brainstorm`, `include_in_investigation`, `include_in_implementation`, `include_in_research_exploration`, `include_in_refactoring_planning`. Each maps to one phase in `_PHASE_REGISTRY` (`ai_providers.py`, re-exported into `protocol_engine.py`) and gates whether that phase dispatches provider Task agents.
- `ai_providers.timeout` — currently **inert** (default `300`): not read by the wrappers, which enforce a fixed deadline (codex 300s, agy 330s watchdog). `config_update` still validates it as an integer (`config.py`); the key is kept for a future plumbing task.
- `codex.enabled`, `agy.enabled` — per-CLI master switches; both `false` in the template.

A provider participates in a phase only when it is in `enabled_providers` **and** the phase's `include_in_*` flag is true **and** its `<provider>.enabled` is true. See [AI Provider Integration](pages/subsystems/ai-provider-integration.md).

### Code review and the optional test gate
- `code_review.enforce_warnings` — when `true`, code-review warnings must be acknowledged before completion (default `false`). Read via the `config_code_review_enforcement` MCP tool.
- `test_command` — optional runner for the `verify_tests_pass` gate on the `code-review` step. Default `null`. Detailed below.

### Notifications
- `notifications`: `enabled` (default `false`), `mode` (`per_step` / `off`; missing key defaults to `per_step`), `prefix`, and `channels.telegram` (`enabled`, `bot_token`, `chat_id`, `ca_bundle`). `off` silences per-step pings but still delivers the protocol-completion ping.
- **Telegram `chat_id` discovery.** `chat_id` is a bare number the user can't easily look up, so `/team-management:config` fills it via the `notification_discover_telegram_chats` MCP tool instead of asking the user to hand-copy it. The tool (`notification_utils.discover_telegram_chats`) validates the token with `getMe`, then does a NON-mutating `getUpdates` read and returns the distinct `{id, type, title}` chats seen in *recent* updates. **Telegram has no endpoint that lists a bot's groups** — a chat surfaces only if it recently messaged the bot (privacy-mode groups: a command/@mention; DMs: the user pressed Start) or the bot was just added (`my_chat_member`); a configured webhook makes `getUpdates` return 409. The config flow presents the discovered chats, sends a confirmation ping to the chosen one (`notification_discover_telegram_chats(test_chat_id=…)` → `send_telegram_test_message`) so the user verifies it BEFORE saving, then writes `chat_id` via `config_update`. The bot token is read from the per-project token file and is never passed to the tool. Discovery is read-only/ungated; only the `chat_id` write is gated. See [MCP Server](pages/subsystems/mcp-server.md).
- **Telegram TLS verification (`ca_bundle`).** Every Telegram HTTP call (discovery, `send_telegram_test_message`, and live `TelegramChannel.send`) builds its SSL context via `notification_utils._telegram_ssl_context()`, which tolerates a Python whose default OpenSSL trust store is empty — e.g. a python.org macOS build where *Install Certificates.command* was never run (the exact failure the plugin's own MCP venv hit). Precedence: explicit `channels.telegram.ca_bundle` (or the `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` env vars) → the platform default context **iff** it already has trust anchors (`cert_store_stats`) → `certifi` (already shipped in the venv via `requests`) → `/etc/ssl/cert.pem` → last-resort default; it never raises. Relatedly, `_telegram_api_call` returns `(payload, error, kind)` and classifies a TLS/transport failure distinctly from an HTTP auth rejection — a certificate-verification failure is surfaced with a CA-fix hint (run *Install Certificates.command*, or set `ca_bundle`), **never** as "Telegram token rejected". `ca_bundle` is an optional string (default empty); leave it empty unless verification fails (empty CA store, or a TLS-inspecting proxy whose root you point it at).

### Context preservation
- `auto_compact.enabled` — automatic context compaction toggle (default `true`).
- `auto_compact.threshold` — token-usage percentage that triggers the compaction directive (default `85`). Read in `post-tool-use.py` with a literal `85` fallback.
- `auto_compact.context_limit` — explicit token budget for the active model (e.g. `1000000` for 1M, `200000` for 200k). It is the **highest-priority** source in `get_model_context_limit()` (`shared_state.py`): config override first, then model-name heuristics (`[1m]`, `(1M context)` → 1,000,000), then a `200000` default. The compaction directive fires when `usage_pct >= threshold` (`post-tool-use.py`).

See [Context Preservation](pages/topics/context-preservation.md) for the full compaction flow.

### Optimize-protocol frontmatter (not a config key)
`_comment_optimize_frontmatter` documents engine-managed keys (`optimize.best_commit`, `optimize.baseline_commit`, `runs_per_iteration`, etc.) that live in **task-file frontmatter**, not in `config.json`. They are written by `_func_capture_metric_baseline` / `_func_update_best_commit` / `_handle_loop_iteration`. See [Optimize Protocols](pages/protocols/optimize-protocols.md).

## `test_command` — allowlist, metacharacter rejection, sandboxed execution

`test_command` is the most security-sensitive config value because it is the only one the engine shells out with. It is validated and executed by `_func_verify_tests_pass` (`protocol_engine.py`), which delegates the validation to the shared `_validate_run_command(cmd, allowed_prefixes)`. Mechanics:

1. **Graceful skip.** `null`, missing, empty/whitespace string, an unreadable config, or a non-dict config all skip the gate — a fat-finger edit elsewhere in `config.json` must not block completion.
2. **Raw-string metacharacter scan, BEFORE tokenisation**. Rejects any of `_TEST_CMD_FORBIDDEN_CHARS = (';', '&&', '||', '|', '` `` `', '$(', '>', '<')`. The order is load-bearing: `shlex.split('pytest;')` yields the single token `'pytest;'`, so a post-split scan would never see the `;`. The scan therefore runs on the raw string first.
3. **Prefix allowlist, word-boundary match**. The command must `==` a prefix or `startswith(prefix + " ")`. `_TEST_CMD_ALLOWED_PREFIXES`: `pytest`, `npm test`, `cargo test`, `go test`, `rspec`, `rake test`, `python -m pytest`, `python -m unittest`, `python3 -m pytest`, `python3 -m unittest`, `jest`. The word-boundary rule means `pytesting` is rejected (it neither equals `pytest` nor starts with `pytest ` ). Bare `ruby` was deliberately excluded because `ruby -e "<arbitrary>"` would pass both gates and execute inline code under `shell=False`; only dedicated runner binaries (`rspec`, `rake test`) are accepted.
4. **Tokenise then run.** Only after both gates pass is the string `shlex.split` and run via `subprocess.run(..., shell=False, timeout=TEST_TIMEOUT)` with `TEST_TIMEOUT = 600` (`engine_constants.py`). `shell=False` eliminates injection even if the allowlist is later relaxed. Non-zero exit or timeout blocks advance.

The same `_validate_run_command` helper guards the optimize protocol's metric command against `_METRIC_CMD_ALLOWED_PREFIXES` (`optimize_completion.py`), so the RAW-STRING-FIRST discipline is shared across every engine func that shells out.

## Deprecated keys

Three keys are deprecated and **never auto-forwarded** to their replacements — migration is user-driven:
- `ai_providers.include_in_architecture`
- `ai_providers.include_in_exploration` (replaced by `include_in_research_exploration`)
- `gemini.default_model` (the framework no longer overrides the Gemini model; it uses the CLI default)

`session-start.py` reads these *only to surface a warning*. When any is present it emits a one-time context notice naming each key and writes `.claude/state/ai-providers-migration-warned.flag`; the flag is cleared at session-start (alongside the compact flags) so a fresh session re-evaluates. The keys are no longer auto-stripped — the retired installer's strip-on-write went away with it in #7; removal is user-driven. The values themselves are inert — the framework does not honor `include_in_exploration`'s value, so leaving the old key set has no behavioral effect beyond the warning.

### Retired provider: `gemini.*`

The Gemini provider was replaced by the Antigravity CLI (`agy`). `gemini.*` is now a **dead key** — its values are never auto-forwarded to `agy`. A **separate** warning fires (`[AI providers — gemini replaced by agy]`, flag `.claude/state/ai-providers-gemini-replaced-warned.flag`) when `gemini.enabled: true` or `"gemini"` appears in `enabled_providers`. These remnants are no longer auto-cleaned — the retired installer's `enabled_providers` strip, the `gemini: {enabled: false}` rewrite, and the stale-`gemini-*.md`-file retirement on upgrade went away with the installer in #7; removal is user-driven.

## Design decisions and rationale

- **Single `issue_tracking.provider` selector with a legacy fallback chain.** Newer configs declare the active provider once; the per-block `enabled` scan exists purely for backward compatibility with configs predating the unified key (`config.py`). This is also why a missing `issue_tracking.provider` key routes completion through the provider chain, not the disabled-mode menu.
- **Defensive reads everywhere.** No central schema validator exists; each consumer owns its defaults via `.get(..., default)`. The cost is duplicated default constants (e.g. the `85` threshold literal in `post-tool-use.py`), the benefit is that a partial or hand-edited config never hard-fails a hook.
- **`_comment_*` keys instead of an external doc.** Rationale lives next to the value it explains, surviving installer overwrites of the template.
- **Allowlist + `shell=False` over a sandbox.** `test_command` accepts user-supplied strings but constrains them to known test runners and runs without a shell, trading flexibility (no pipelines, no inline `ruby -e`) for injection safety.
- **Deprecation warning, not auto-migration.** Auto-forwarding `include_in_architecture: true` under the old code-review-only meaning to the new 6-phase model would silently enable phases the user never asked for; surfacing the keys lets the user move them deliberately.

## Gotchas

- **Mtime caching can serve stale config.** The MCP server only reloads when `st_mtime` changes (`config.py`). Editing and re-saving fast enough to preserve the mtime, or touching it on a filesystem with coarse mtime granularity, can serve the cached dict. Hooks re-read per call and are not affected.
- **A broken `config.json` is silent on the MCP side.** A JSON parse error returns `{}` (`config.py`), so the MCP server behaves as if *nothing* is configured rather than reporting the error. Symptom of a malformed file is "provider suddenly disabled," not an error message.
- **`provider` set but block not enabled = no provider.** `issue_tracking.provider: "github"` with `github.enabled: false` does **not** activate GitHub; `detect_provider()` requires the matching block's `enabled` to be truthy (`config.py`), then falls through the legacy scan, which also finds nothing.
- **`test_command` is the only config-driven shell-out.** Anything not on `_TEST_CMD_ALLOWED_PREFIXES` is rejected even if it is a legitimate runner (e.g. `tox`, `make test`, `bin/rails test`). Extending it means editing the tuple in `protocol_engine.py`, not the config.
- **`context_limit` overrides model heuristics.** A wrong `auto_compact.context_limit` (e.g. `200000` left on a 1M-context model) makes compaction trigger far too early, because the explicit override wins over the `[1m]` name heuristic (`shared_state.py`).
- **`_comment_*` keys are not validated.** They can be edited or deleted with no effect; do not rely on them being present programmatically.
- **Deprecated-key values are dead weight.** Setting `include_in_exploration: true` does nothing except trigger the one-time deprecation warning; the real flag is `include_in_research_exploration`.
