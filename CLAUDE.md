# team-management CLAUDE.md

## Purpose
Complete team-management framework that enforces Discussion-Alignment-Implementation-Check (DAIC) methodology for AI pair programming workflows.

## Narrative Summary

The team-management package transforms Claude Code from a basic AI coding assistant into a sophisticated workflow management system. It enforces structured collaboration patterns where Claude must discuss approaches before implementing code, maintains persistent task context across sessions, and provides specialized agents for complex operations.

The core innovation is the DAIC (Discussion-Alignment-Implementation-Check) enforcement through Python hooks that cannot be bypassed. DAIC mode is driven by the JSON protocol engine, which sets discussion, implementation, or documentation mode automatically per workflow step; it can also be switched manually via the `daic_mode_switch_*` MCP tools. In discussion mode the hooks block edit tools and require alignment before implementation proceeds. This prevents the common AI coding problem of immediate over-implementation without alignment.

The framework includes persistent task management with git branch enforcement, context preservation through session restarts, specialized subagents for heavy operations, and automatic context compaction when approaching token limits.

## Key Files

### Plugin Runtime & Configuration (the pip/npm installer was retired in #7)
- `plugin/.claude-plugin/plugin.json` - Plugin manifest wiring hooks / MCP server / commands / agents (version in sync with marketplace.json); no longer declares `userConfig` — provider tokens moved to a per-project, user-authored `.claude/state/provider-tokens.json` (the OS-keychain model was retired because the keychain is global-per-plugin-per-user, so two projects could not use different tokens). Marketplace descriptor: `.claude-plugin/marketplace.json` → `./plugin`
- `plugin/hooks/shared_state.py` - Three-root path model (`get_project_root()` / `get_plugin_root()` / `get_plugin_data()`), shared state helpers, the `file → config.json` `resolve_provider_token` (reads the per-project `.claude/state/provider-tokens.json`; config.json is a legacy fallback), and `ensure_provider_tokens_file` (create-if-absent `0600` seeder for that file)
- `plugin/hooks/_shim.py` - Stdlib launcher shim every hook runs through (`python3 ${CLAUDE_PLUGIN_ROOT}/hooks/_shim.py <hook>`); selects the venv python if built, else system python
- `plugin/hooks/hooks.json` - Hook event → shim-command registrations (merge-friendly; the host merges them with the project's `.claude/settings.json`)
- `plugin/mcp/bootstrap_mcp.py` - MCP cold-start: builds/validates a venv under `${CLAUDE_PLUGIN_DATA}` keyed to the `requirements.lock` hash, then `os.execv`s the venv python running `server.py`
- `plugin/hooks/boot_detector.py` - Guards against a legacy pip install coexisting with the plugin (SessionStart advisory + PreToolUse hard block)
- `plugin/hooks/hook_utils.py` - Shared hook helpers incl. module-level `normalize_command()` (hook-command canonicalisation; consumed by `boot_detector.py`)
- `plugin/commands/config.md` / `plugin/commands/init.md` - In-Claude-Code configuration slash commands that replaced the old install.py prompt flow
- `plugin/mcp/tools/config.py` - `config_get` / `config_update` MCP tools backing the config flow. `config_get` also returns a `schema` catalog (`{key, type, allowed?, description}` per settable key, on all return paths) so the flow knows each key's exact type/enum; the `_CONFIG_SCHEMA` allowlist covers the full non-secret settable surface incl. `code_review.enforce_warnings`, `notifications.*`, `features.icon_style` (m-fix-config-schema-exposure)
- `plugin/hooks/config_intent_gate.py` - Deterministic intent-gate hook routing config edits through the sanctioned flow
- `plugin/agents/codex-cli.md` - Wrapper agent that runs `codex review --uncommitted` / `codex exec -s read-only` in a sandboxed Task
- `plugin/agents/agy-cli.md` - Wrapper agent that runs `agy --add-dir "$PWD" --dangerously-skip-permissions --print-timeout 300s -p ...` (NO `--sandbox`) in a Task, contained by a project-local `.agents/hooks.json` read-only deny-gate
- `plugin/templates/agy-readonly-gate.py` - Stdlib PreToolUse deny-gate deployed into a project's `.agents/hooks.json` (named hook `team-management-readonly-gate`); hard-`deny`s every agy tool call outside a read-only allowlist. Deployed by `shared_state.ensure_agy_readonly_gate_deployed` (merge-aware, refresh-on-change) when agy is enabled

### Core Framework
- `plugin/mcp/server.py` - MCP server entry point (imports and tool registration only)
- `plugin/hooks/sessions-enforce.py` - Core DAIC enforcement with corrected execution order (subagent bypass and admin whitelist before DAIC)
- `plugin/hooks/session-start.py` - Automatic task context loading
- `plugin/hooks/user-messages.py` - Read-only task/protocol hints and context monitoring (no mode switching — DAIC mode is engine/MCP-driven)
- `plugin/hooks/post-tool-use.py` - Auto-compact token monitoring, protocol end-condition injection (throttled), auto work-log appends during the implementation step, and multi-provider auto-sync
- `plugin/hooks/pre-compact.py` - PreCompact hook for state preservation before context compaction
- `plugin/hooks/gitlab_utils.py` - GitLab API wrapper (single source of truth for all imports)
- `plugin/hooks/jira_utils.py` - Jira API wrapper (single source of truth for all imports)
- `plugin/hooks/github_utils.py` - GitHub / Gitea API wrapper (single source of truth for all imports)
- `plugin/hooks/shared_state.py` - Shared state management across hooks
- `plugin/hooks/__init__.py` - Package marker making `plugin.hooks` importable; `plugin/hooks/workflow_command.py` holds the `team-workflow` entry logic (the old pip console-script wiring was retired with the installer in #7)
- `plugin/hooks/ai_providers.py` - `AIProvidersMixin` + `_PHASE_REGISTRY` / credential-filter / template-loader globals (6-phase AI-provider dispatch; the registry is the single source of truth, imported by `protocol_engine.py`)
- `plugin/hooks/optimize_completion.py` - `OptimizeCompletionMixin`: optimize-protocol funcs + the unified completion dispatcher used by all protocols; module-level `_bounded_regex_search` runs the user-supplied `metric_parser` regex in a killable child process (5s timeout, 256K-char stdout cap — catastrophic-backtracking guard; a daemon-thread timeout cannot work since stdlib `re` holds the GIL for the whole match), `_audit_path_matches_frozen` gives the policy audit component-boundary frozen-path matching, and `_squash_to_best` aborts before any reset when the rollback-point capture fails (l-optimize-robustness-cleanup)
- `plugin/hooks/engine_constants.py` - Shared subprocess timeouts (`GIT_TIMEOUT_*`, `TEST_TIMEOUT`) imported by the engine and its mixins
- `plugin/mcp/tools/daic.py` - DAIC mode switching MCP tools (primary interface)
- `plugin/agents/logging.md` - Session work log consolidation agent
- `plugin/templates/CLAUDE.tm.md` - Plugin-owned behavioral guidance template; carries a top-of-file "do not edit — edit CLAUDE.tm.custom.md instead" rule and is replaced on every update
- `plugin/templates/CLAUDE.tm.custom.md` - User-owned customization stub; created once and NEVER overwritten on update (where users put custom rules / custom protocols)
- `plugin/templates/CLAUDE.wiki.md` - LLM Wiki behavioral template (operations, page format, page organization by category, read+self-verify usage, security guidance)
- `plugin/templates/wiki-schema.md` - Wiki schema seed template (focus, page types, format conventions)
- `plugin/templates/statusline.py` - Statusline source (deployed to `team-management/statusline.py`); relocated from the now-removed legacy `sessions/` directory. Line 2 renders a project-name segment between the open-tasks and MCP segments — the configured `project_name` config key, falling back to the project folder name (`PROJECT_ROOT.name`) when empty/unset (m-statusline-project-name). Runs on every prompt, so transcript reads are **bounded tail-reads** (m-statusline-and-test-infra): `find_current_transcript` uses `shared_state.read_last_jsonl_entry` (seek-from-end) instead of `readlines()`-ing the whole session JSONL. The MCP segment is honest — in plugin mode it surfaces the plugin's own server under its full name `team-management` (deduped against a same-named project entry) and no longer paints a fake `(✓) connected` marker it cannot verify. The dead pip-era `else` import branch was removed (replaced by a loud `ImportError`), the plugin-wrong error hint dropped, and the bare `except:` clauses narrowed
- `plugin/templates/config.template.json` - Configuration template with multi-provider settings; the `team-management/config.template.json` path session-start.py's recovery guidance expects (renamed from `sessions-config.template.json`)
- `plugin/commands/wiki-ingest.md` - Slash command (`/team-management:wiki-ingest`): ingest a raw file into wiki pages
- `plugin/commands/wiki-tune.md` - Slash command (`/team-management:wiki-tune`): interactively customize wiki/schema.md
- `plugin/commands/wiki-lint.md` - Slash command (`/team-management:wiki-lint`): health-check wiki for orphans, broken links, quality issues
- `plugin/commands/custom-protocol-create.md` - Slash command: fork a system protocol into `custom/` for editing (drives `protocol_customize`)
- `plugin/commands/custom-protocol-update-after-reinstall.md` - Slash command: reconcile custom protocols with system after an update (drives `protocol_check_drift`)
- `plugin/knowledge/claude-code/hooks-reference.md` - Hook system documentation
- `docs/MCP_SERVER.md` - MCP server architecture, setup, and usage documentation

## Installation (Claude Code plugin)
team-management ships as a native Claude Code plugin (pip/pipx/npm distribution was retired in #7).
- **Marketplace**: install via the marketplace descriptor `.claude-plugin/marketplace.json` (→ `./plugin`)
- **Local / development**: launch Claude Code with `--plugin-dir ./plugin`
- First run cold-starts automatically: `plugin/mcp/bootstrap_mcp.py` builds the venv under `${CLAUDE_PLUGIN_DATA}` and the hooks run through `plugin/hooks/_shim.py`
- Configure in-session with `/team-management:init` then `/team-management:config` (no install-time prompts)

## Core Features

### DAIC Enforcement
- Three modes: discussion (blocks edit tools), implementation (full access), documentation (docs-only edits)
- DAIC mode managed by protocol engine (automatic per-step) or MCP tools (manual override)
- Protected paths prevent direct state file manipulation
- Read-only Bash commands allowed in discussion mode

### Task Management
- Priority-prefixed tasks: h- (high), m- (medium), l- (low), r- (research/investigate), o- (optimize), b- (brainstorm)
- Automatic git branch creation and enforcement
- Persistent context across session restarts
- Work log consolidation and cleanup
- GitLab issue import as structured Claude tasks
- Bidirectional task-issue synchronization with status mapping
- Workflow protocols: **task** (standard lifecycle), **brainstorm** (parallel-specialist ideation), **research** (spike/PoC/evaluation), **refactoring** (test-baseline-gated), **optimize** (interactive batched metric-driven optimization with checkpoints between batches; squashes from `optimize.best_commit` and ships a leaderboard MR/PR description on completion), **optimize-unattended** (autonomous twin of `optimize`; experimentation runs unattended to a termination condition with no batch checkpoints; designed for overnight runs)

### Branch Enforcement
- Task-to-branch mapping: implement- → feature/, fix- → fix/, o- → optimize/, etc. (see `DEFAULT_CONFIG.branch_prefixes` in sessions-enforce.py)
- Blocks code edits if current branch doesn't match task requirements
- Four failure modes: wrong branch, no branch, task missing, branch missing
- Automated merge request creation linked to GitLab issues

### Context Preservation
- Auto-compact: triggers context compaction protocol at configurable threshold (default 85%)
- PreCompact hook saves checkpoint (task, branch, protocol, DAIC mode) before native compaction
- Post-compact restoration injects session state summary after compaction completes
- Token warnings at 80%/90% thresholds (fallback when auto-compact disabled)
- Session restart with full task context loading
- Specialized agents operate in separate contexts

### Multi-Provider Issue Tracking Integration
- **GitLab Support**: Full integration with GitLab issues and merge requests
- **Jira Support**: Complete Jira API integration with markdown-to-wiki conversion
- **GitHub / Gitea Support**: Unified provider for GitHub and self-hosted Gitea with automatic API detection
  - **Gitea Detection**: Automatic Gitea identification from URLs (domain, path patterns)
  - **API Compatibility**: Gitea uses GitHub-compatible v3 API with label ID extensions
  - **Label Management**: Smart label handling (names for GitHub, IDs for Gitea with auto-creation)
  - **Frontmatter Stripping**: Automatic removal of YAML frontmatter when importing Gitea-created issues
  - **Update Mode**: Re-fetch and update existing task files from issues
  - **URL Parsing**: Gitea is detected at runtime from the configured `base_url` (`github/api.py::is_gitea`); `base_url` itself is supplied directly via the config flow. (The pip-era installer's repository-URL auto-detect parser was retired in #7.)
- **Issue Import**: Convert provider issues to Claude tasks with full context preservation
- **Issue Creation**: Generate provider issues from Claude tasks with proper formatting
- **Status Synchronization**: Automatic sync of task status to provider issue states
- **CI/CD Workflow**: Complete workflow from task completion to merge/pull request creation (GitLab/GitHub/Gitea)
- **Provider-Disabled Completion Menu**: When `issue_tracking.provider` is explicitly `"disabled"`, the completion/planning step presents a 4-option menu (`merge_local` / `push_pr` / `keep` / `discard`) dispatched by `completion_dispatch`. All protocols with git operations use this dispatcher: task, brainstorm (planning step), refactoring, optimize, optimize-unattended. Provider-driven completion behaviour is unchanged for GitLab/GitHub/Jira users. **A config with no `issue_tracking.provider` key AND no issue provider enabled now INFERS `disabled`** (m-fix-completion-strands-without-remote) → it gets the 4-option menu, so a fresh plugin project with no tracker gets local completion instead of the remote provider chain (which strands a no-remote repo). The provider chain is still run for a config with a provider actually enabled but no `provider` key (genuinely-old install), a non-dict `issue_tracking` (corruption), or an unreadable `config.json`. As a safety net, `git_merge_main` / `git_push` skip gracefully (`action:"skipped"`) when there is no `origin` remote instead of hard-failing on `git fetch`/`git push`. `push_pr` uses `gh pr create` with a `gh pr view` idempotency precheck. The `discard` path is gated by a two-step typed confirmation (`discard_confirmation="discard"` + `discard_confirmed_dry_run=true`) — **friction, not security** (an LLM can trivially produce the string; the gate exists to slow accidental invocation). `discard`'s `git clean -fdx -e team-management/ -e .claude/` (h-fix-discard-clean-and-windows-transcript) excludes the framework working tree, so it never wipes config / issue-mappings / logs / sibling tasks / custom protocols — only the task's source work is discarded. Branch-safety precondition: `HEAD` must match the task's feature branch before any local flow runs. Default-branch detection: `origin/HEAD` symbolic-ref → candidate probe → `"main"` fallback.
- **Mandatory Code Review**: Integration with code review before issue closure
- **Mapping Management**: Persistent task-to-issue relationships with sync metadata per provider
- **Auto-sync**: Optional automatic synchronization on task lifecycle events
- **MCP Server**: Model Context Protocol server exposing issue tracking as first-class tools
- **Structured Error Reporting**: Detailed error responses with HTTP status, response bodies, URLs, and error types for debugging
- **Error-Body Token Redaction**: Shared `IssueTrackingProvider._redact_token` (in `issue_provider_base.py`) scrubs the configured API token from response bodies in all three providers' `_make_request` HTTPError handlers — redacted before the 500-char truncation so a self-hosted instance echoing the token cannot leak it into exceptions/logs

### AI Provider Integration
Multi-model code analysis through external AI providers running in parallel with Claude agents.

- **Configuration**: AI providers are enabled per-phase in `team-management/config.json` (`ai_providers.enabled_providers` + the six `include_in_<phase>` flags + integer `timeout`, default 300), edited in-session via `/team-management:config` (the `config_update` MCP tool) — the old install-time detection/Y-n flow was retired with the installer in #7. Each enabled phase makes its protocol step wait on the provider call (token cost + per-call latency); the wrappers enforce a fixed deadline (codex 300s, agy 330s watchdog) — the `timeout` key is currently inert (not read by the wrappers). Adding a new provider = a new wrapper agent under `plugin/agents/` plus a `_PHASE_REGISTRY`-aware codepath (see below).
- **Codex Provider**: OpenAI Codex integration via the `codex` CLI run as a parallel Task agent
  - Pass-through wrapper at `plugin/agents/codex-cli.md` (caller owns the full prompt). Invokes `codex review --uncommitted` for review or `codex exec -s read-only` for exec; read-only sandbox; full repository access. Carries a shell-native fallback watchdog on hosts without `gtimeout`/`timeout` (both invocation sites — plain + `--output-schema`): backgrounds codex → `( sleep 300; kill "$CODEX_PID" )` → SIGTERM at the deadline → `codex review unavailable: timed out after 300s (watchdog)`; a sub-128 non-zero exit surfaces a `[wrapper] codex exit code: <rc> (non-zero — codex failed)` stderr marker so the graceful `unavailable:` reply still fires. `env -i PATH HOME` scrub on both branches; drift-guard `test/test_codex_watchdog.py`. (m-fix-ai-provider-wrapper-timeout)
- **Agy Provider**: Google Antigravity CLI integration via the `agy` CLI run as a parallel Task agent
  - Pass-through wrapper at `plugin/agents/agy-cli.md`. Invokes `agy --add-dir "$PWD" --dangerously-skip-permissions --print-timeout 300s -p "$PROMPT"` — **NO `--sandbox`** (on macOS the sandbox blocks git's `$TMPDIR`/xcrun-cache write, so every git command fails and agy produces no review). `--add-dir "$PWD"` binds the repo as agy's workspace; `--dangerously-skip-permissions` is the only reliable way past agy's headless print-mode soft-deny. External watchdog 330s: gtimeout/timeout when available, otherwise a shell-native fallback — backgrounded agy with output to a mktemp file + EXIT trap, detached `( sleep 330; kill "$AGY_PID" ) >/dev/null 2>&1 &` subshell, watchdog cleanup after wait, and a `[wrapper] agy exit code: <rc> (likely watchdog kill)` stderr line when rc>=128 with reply contract `agy review unavailable: timed out after 330s (watchdog)`; drift-guard `test/test_agy_watchdog.py`. **skip-permissions is CONTAINED by a project-local `.agents/hooks.json` PreToolUse read-only deny-gate** (`plugin/templates/agy-readonly-gate.py`, named hook `team-management-readonly-gate`): a `matcher: "*"` hook that hard-`deny`s every tool call outside a read-only allowlist (git status/diff/log/show/rev-parse/ls-files/blame + read tools), denying `write_file`/edit, mutating git, `rm`, `curl`, shell metachars, and write/exec flags (`find -exec`/`-fprint0`, `git diff --output`/`--ext-diff`/`--textconv`, `rg --pre`/`--hostname-bin`). The gate is deployed by `shared_state.ensure_agy_readonly_gate_deployed`. The wrapper's **preflight** deep-equals the canonical hooks.json entry and refuses to run (`agy review unavailable: read-only gate not deployed or altered`) if the gate is missing/tampered — it never runs uncontained. Belt-and-suspenders on top of the gate, the wrapper's mutation check compares a before/after `git status --porcelain --untracked-files=all` snapshot AND a before/after `git diff HEAD | cksum` content-hash (porcelain catches new/deleted/newly-dirty paths; the content-hash catches in-place edits to files ALREADY dirty before the run) and prepends `agy review WARNING: agy modified files during read-only run: <paths>` on any diff (detect & report, never auto-revert). `env -i PATH HOME` scrub unchanged. Framework does not override the agy model — uses CLI default; output shape is owned by the caller's prompt. The wrapper never touches `~/.gemini/` config (a malformed permissions rule hangs agy print mode indefinitely). **Accepted limitation:** config-driven `git diff` external drivers (`.git/config` `diff.external`, `.gitattributes` textconv) execute inside git on a plain `git diff` with no flag — the gate cannot intercept this; safe for the intended own-repo use, do not point agy at an untrusted repo's `.git/config`.
  - Requires: Antigravity CLI installed and authenticated (run `agy` once to sign in)
- **Registry-Driven 6-Phase Model**: `_PHASE_REGISTRY` in `plugin/hooks/ai_providers.py` (imported by `protocol_engine.py`) is the single source of truth for the 6 AI provider phases — `code_review`, `brainstorm`, `investigation`, `implementation`, `research_exploration` (renamed from `exploration`), `refactoring_planning`. Each entry: `func_name`, `config_flag`, `description`, `companion`, `template_subpath`, `subcommand` (`review`/`exec`, governs the sandbox-flag check), `protocol_json_steps`. `_build_handlers` and `get_available_funcs` iterate the registry; adding a future phase = one row + a 3-line `_func_resolve_ai_providers_for_<phase>` method + two markdown templates under `plugin/protocol-configs/providers/`.
  - `include_in_code_review` - Enable in task `code-review` step
  - `include_in_brainstorm` - Enable in brainstorm `analysis` step (parallel with specialist agents)
  - `include_in_investigation` - Enable in task `investigation` step
  - `include_in_implementation` - Enable in task `implementation` step
  - `include_in_research_exploration` - Enable in research `exploration` step (renamed from `include_in_exploration`)
  - `include_in_refactoring_planning` - Enable in refactoring `planning` step
  - `timeout` - Currently inert (default 300): not read by the wrappers, which enforce a fixed deadline (codex 300s, agy 330s watchdog). Kept for a future plumbing task.
- **Template-Driven Prompts**: 5 of 6 phases (all except `code_review`, which keeps inline prompts) load from `protocol-configs/providers/<codex|agy>-<phase>.md` (10 markdown templates total — codex-implementation uses Codex R3 shape). 4-tier lookup via `_load_provider_template`: custom override → system install → package source → inline default + stderr warning. `_DefaultEmptyDict` prevents `KeyError` on missing context vars.
- **Credential Filter**: `_CREDENTIAL_FILTER_PATTERNS` (17 named regex patterns: 8 base + 4 Gemini-suggested + 4 value-format — `github-pat` incl. fine-grained `github_pat_`, `slack-token`, `aws-access-key-id` (AKIA|ASIA, case-exact), `jwt` — plus the SEC-003a `plugin-option` pattern) applied line-by-line to task descriptions BEFORE injection into provider prompts. Name-based `credentials`/`secret`/`token` patterns are anchored to assignment context (`[:=]` separator); `secret`/`token` match compound names via a `[a-z0-9_]*` prefix. First match wins; whole line replaced with `[REDACTED:<reason>]`. A stateful PEM pass (`_PEM_BEGIN_RE`/`_PEM_END_RE`) redacts an entire private-key body through the END line as `[REDACTED:private-key]`; arming is independent of first-match-wins (a BEGIN line that also matches an earlier pattern still arms). Known accepted gap: a single physical line containing END before BEGIN does not arm. Defense-in-depth for the task-description channel only — the codebase itself is read by the provider's own CLI sandbox.
- **Sandbox-Flag Check**: codex `exec` paths must carry literal `-s read-only` in the prompt (`codex review` skips — has its own sandbox); agy paths must carry `--dangerously-skip-permissions` and must NOT carry `--sandbox` (containment is the `.agents/hooks.json` read-only deny-gate, not the OS sandbox). Enforced by module-level `_ensure_sandbox_flags(...)` raising the dedicated `SandboxFlagError(ValueError)` (raise-based, survives `python -O` — replaced the former bare `assert`s). The shared `_resolve_ai_providers` dispatcher re-raises ONLY `(AssertionError, SandboxFlagError)` — deliberately narrow, because `json.JSONDecodeError` and `UnicodeDecodeError` are ValueError subclasses that must fall to the graceful catch-all (malformed config.json never blocks a protocol step).
- **Legacy Key Deprecation**: `ai_providers.include_in_architecture`, `ai_providers.include_in_exploration`, and `gemini.default_model` are deprecated. `session-start.py` (around line 117-153) emits a one-time context warning when any are present and writes `.claude/state/ai-providers-migration-warned.flag`. **Values are NEVER auto-forwarded** — migration is user-driven; the keys are no longer auto-stripped (the retired installer's strip-on-write went away with it in #7).
- **Gemini-Replaced Migration**: The Gemini provider was retired in favour of `agy` (Google Antigravity CLI). `gemini.*` is now a dead key — values are NEVER auto-forwarded to `agy`. `session-start.py` emits a one-time `[AI providers — gemini replaced by agy]` warning when `gemini.enabled: true` OR `"gemini"` appears in `enabled_providers`, writing `.claude/state/ai-providers-gemini-replaced-warned.flag` (separate from `ai-providers-migration-warned.flag`). These remnants are no longer auto-cleaned — the retired installer's `enabled_providers` strip, the `gemini: {enabled: false}` rewrite, and the stale-file retirement on upgrade all went away with the installer in #7; removal is user-driven.
- **Parallel Execution**: AI provider calls run alongside Claude agents for multi-perspective analysis
- **Fixed wrapper deadline**: the wrappers enforce a fixed deadline (codex 300s, agy 330s watchdog); the `ai_providers.timeout` config key is currently inert (not read by the wrappers). Other time budgeting is delegated to the surrounding Task agent and providers' internal limits.
- **Graceful Degradation**: Wrapper failures return a `<provider> review unavailable: …` block, never block the workflow.

### LLM Wiki
Persistent, compounding knowledge base maintained by Claude. The user curates source documents in `wiki/raw/`; Claude processes them into structured wiki pages.

- **Config-Flow Setup**: `/team-management:config` toggles `wiki.enabled` and, when `wiki/` is absent, offers to seed the structure (index.md, log.md, schema.md, raw/README.md); seeding is idempotent. (The pre-plugin installer's wiki setup was retired in #7.)
- **CLAUDE.wiki.md**: Behavioral template defining wiki operations, page format, page organization by category, cross-reference conventions, the read+self-verify usage rule, and security guidance for `wiki/raw/`
- **wiki-schema.md**: Domain-specific schema seed (focus areas, page types, `## Categories` list, format conventions)
- **Page Storage (nested by category)** (m-wiki-nesting-read-verify-doc-reminder): pages live in category subdirectories — `wiki/pages/<category>/<slug>.md`. Categories are HYBRID: `wiki/schema.md` `## Categories` lists the known ones; ingest may propose a new one (with user confirmation) and create its directory lazily. Flat `wiki/pages/<slug>.md` pages remain valid (backward-compatible). Cross-references and index entries use **wiki-root-relative** paths (`pages/<category>/<slug>.md`), and `wiki/index.md` mirrors the structure with a `## <Category>` heading per category. The same slug may appear in different categories (addressed by full path).
- **Read + Self-Verify usage** (m-wiki-nesting-read-verify-doc-reminder): when a `wiki/` exists it is the first place to look for project knowledge — consult `wiki/index.md` → relevant pages before broad code search, follow the pages' symbol/file code references to the real source, treat CODE as ground truth, and continuously self-verify (fix small/unambiguous drift in place bumping `updated:`; flag large/ambiguous drift). Ambient guidance in `CLAUDE.wiki.md`; the `context-gathering` agent also consults the wiki first (gated on `wiki/` existence, no-op otherwise).
- **Slash Commands**: Three dedicated commands for wiki operations
  - `/team-management:wiki-ingest <file>` - Process a raw file into structured wiki pages with discussion; picks/creates a category and writes `wiki/pages/<category>/<slug>.md`
  - `/team-management:wiki-tune [section]` - Interactively customize wiki/schema.md (incl. the `## Categories` list) via Q&A
  - `/team-management:wiki-lint` - Audit wiki/pages/ (nested-aware) for orphans, broken links, and quality issues (delegates to subagent for large wikis)
- **DAIC Whitelist**: `wiki/` directory is unconditionally whitelisted in sessions-enforce.py administrative whitelist, allowing wiki edits in any DAIC mode
- **Documentation-Step Reminder** (enriched in m-wiki-nesting-read-verify-doc-reminder): `wiki_update_reminder` pre_func injects an enriched capture prompt during the task protocol's documentation step (what to capture, which `pages/<category>/`, update index + log, no CLAUDE.md duplication, skip-rule). This is now the sole home of the reactive "update the wiki while working" guidance — the former ambient `## Update Guidance` section in `CLAUDE.wiki.md` was removed, so that prompt no longer sits in every wiki-enabled session's loaded context.
- **Configuration**: Enabled via `wiki.enabled` in team-management/config.json (default: disabled)

### Specialized Agents
- **context-gathering**: Creates comprehensive task context manifests
- **logging**: Consolidates work logs with cleanup and chronological ordering
- **code-review**: Reviews implementations for quality and patterns
- **code-cleanliness**: Analyzes code for cleanliness and maintainability issues
- **context-refinement**: Updates context with session discoveries
- **service-documentation**: Maintains CLAUDE.md files for services
- **codex-cli**: Pass-through wrapper running `codex review --uncommitted` / `codex exec -s read-only`; caller owns the full prompt
- **agy-cli**: Pass-through wrapper running `agy --add-dir "$PWD" --dangerously-skip-permissions --print-timeout 300s -p ...` (NO `--sandbox`); framework uses CLI default model. Containment is a project-local `.agents/hooks.json` read-only deny-gate (`team-management-readonly-gate`); the wrapper preflight-refuses to run if the gate is missing/tampered. Mutation check compares a before/after `git status --porcelain` snapshot AND a `git diff HEAD | cksum` content-hash (the latter catches in-place edits to already-dirty tracked files) and prepends an `agy review WARNING: agy modified files...` line on diff (detect & report); returns `agy review unavailable: <reason>` on failure

## Integration Points

### Consumes
- Claude Code hooks system for behavioral enforcement
- Claude Code MCP server system for tool exposure
- Git for branch management and enforcement
- Python 3.10+ with tiktoken for token counting
- FastMCP SDK for MCP server implementation (optional)
- Shell environment for command execution (Bash/PowerShell/Command Prompt)
- GitLab API for issue tracking and project management
- Jira API for issue tracking and workflow management
- GitHub / Gitea API for issue tracking and pull request management
- Provider webhooks for real-time synchronization (planned)
- External AI provider CLIs invoked as parallel Task agents (Codex `codex`, Antigravity `agy`, future: local LLMs)

### Provides
- MCP Tools (Protocol): `protocol_list`, `protocol_start`, `protocol_current`, `protocol_advance`, `protocol_goto`, `protocol_log`, `protocol_abort`, `protocol_save_note`, `protocol_available_funcs`, `protocol_customize`, `protocol_check_drift`
- MCP Tools (DAIC): `daic_mode_switch_discussion`, `daic_mode_switch_implementation`, `daic_mode_switch_documentation`
- MCP Tools (Issue Tracking): `issue_status`, `issue_read`, `issue_create`, `issue_sync`, `issue_link`, `issue_unlink` (6 of 14 — representative subset)
- MCP Tools (Config): `config_get` (read-only masked config snapshot, ungated), `config_update` (gated, schema-validated writer for non-secret config)
- *The MCP tool lists above are representative, not exhaustive — the full 42-tool / 8-module inventory (incl. code review, git, notifications, and release tools) is enumerated and drift-guarded in `plugin/mcp/CLAUDE.md`.*
- Hook-based tool blocking and behavioral enforcement
- Task file templates and management protocols
- Agent-based specialized operations
- Bidirectional multi-provider task synchronization
- Automated CI/CD workflow with merge request creation

## Configuration

Primary configuration in `team-management/config.json`:
- `developer_name` - How Claude addresses the user
- `project_name` - Optional statusline label (line 2, between open-tasks and MCP); empty/unset falls back to the project folder name
- `protocol_engine.enabled` - Enable/disable JSON-driven protocol engine
- `blocked_tools` - Tools blocked in discussion mode
- `branch_enforcement.enabled` - Enable/disable git branch checking
- `task_detection.enabled` - Enable/disable task-based workflows
- `issue_tracking.provider` - Active provider: "gitlab", "jira", "github", or "disabled"
- `issue_tracking.auto_sync` - Global auto-sync setting
- `gitlab.enabled` - Enable/disable GitLab integration
- `gitlab.api_token` - GitLab API authentication token
- `gitlab.project_path` - GitLab project path (namespace/project)
- `gitlab.base_url` - GitLab instance URL (defaults to gitlab.com)
- `gitlab.auto_sync` - Enable automatic task-issue synchronization
- `gitlab.default_labels` - Default labels for created GitLab issues
- `jira.enabled` - Enable/disable Jira integration
- `jira.api_token` - Jira Personal Access Token
- `jira.base_url` - Jira instance URL
- `jira.project_key` - Jira project key
- `jira.auto_sync` - Enable automatic task-issue synchronization
- `github.enabled` - Enable/disable GitHub / Gitea integration
- `github.api_token` - GitHub or Gitea Personal Access Token
- `github.base_url` - GitHub or Gitea API URL
  - GitHub: "https://api.github.com" (default)
  - Gitea: "https://gitea.example.com/api/v1"
  - Supplied directly in the in-session config flow; Gitea is detected at runtime from `base_url` (`github/api.py::is_gitea`)
- `github.repository` - GitHub / Gitea repository (owner/repo format)
- `github.auto_sync` - Enable automatic task-issue synchronization
- `github.workflow_labels` - Label mapping for workflow states (names for GitHub, auto-converted to IDs for Gitea)
- `ai_providers.enabled_providers` - List of active AI providers (e.g., ["codex", "agy"])
- `ai_providers.include_in_code_review` - Use AI providers in code review phase
- `ai_providers.include_in_brainstorm` - Use AI providers in brainstorm analysis phase
- `ai_providers.include_in_investigation` - Use AI providers in task investigation phase
- `ai_providers.include_in_implementation` - Use AI providers in implementation planning phase
- `ai_providers.include_in_research_exploration` - Use AI providers in research exploration phase (renamed from `include_in_exploration`)
- `ai_providers.include_in_refactoring_planning` - Use AI providers in refactoring planning phase
- `ai_providers.timeout` - Currently inert (default 300): not read by the wrappers, which enforce a fixed deadline (codex 300s, agy 330s watchdog). Kept for a future plumbing task.
- Legacy `ai_providers.include_in_architecture` / `ai_providers.include_in_exploration` / `gemini.default_model` deprecated — values NOT auto-forwarded; session-start emits a one-time deprecation warning and writes `.claude/state/ai-providers-migration-warned.flag`. The keys are no longer auto-stripped — removal is user-driven.
- `codex.enabled` - Enable/disable Codex integration
- `agy.enabled` - Enable/disable Antigravity CLI integration (framework uses CLI default model). Dual-gate with `"agy" in ai_providers.enabled_providers`.
- Legacy `gemini.*` is a retired dead key (Gemini replaced by `agy`) — values NOT auto-forwarded; session-start emits a one-time `[AI providers — gemini replaced by agy]` warning when `gemini.enabled: true` or `"gemini"` is in `enabled_providers`, and writes `.claude/state/ai-providers-gemini-replaced-warned.flag`. These remnants are no longer auto-stripped — removal is user-driven.
- `wiki.enabled` - Enable/disable LLM Wiki feature (default: false)
- `auto_compact.enabled` - Enable/disable automatic context compaction (default: true)
- `auto_compact.threshold` - Token usage percentage to trigger compaction (default: 85)
- `auto_compact.context_limit` - Explicit model context-window budget in tokens (positive int; overrides auto-detection from the model name — e.g. `1000000` for 1M, `200000` for 200k). Settable via `/team-management:config` (added m-fix-plugin-mode-install-bugs)
- `test_command` - **Optional** test-runner command for the `verify_tests_pass` gate on the `code-review` step (null / omitted / empty string → gate skipped gracefully). Must be exactly, or start with (followed by a space), one of the allowlisted prefixes: `pytest`, `npm test`, `cargo test`, `go test`, `rspec`, `rake test`, `python -m pytest`, `python -m unittest`, `python3 -m pytest`, `python3 -m unittest`, `jest`. Shell metacharacters (`;`, `&&`, `||`, `|`, `` ` ``, `$(`, `>`, `<`) are rejected on the raw string before tokenisation; command runs under `subprocess.run(..., shell=False, timeout=600)`. Template in `config.template.json` ships with `test_command: null` plus an explanatory `_comment_test_command` key.

State files in `.claude/state/`:
- `current_task.json` - Active task metadata (PROTECTED — managed by protocol engine/hooks; never edit directly)
- `daic-mode.json` - Current discussion/implementation mode (PROTECTED — managed by protocol engine/hooks; never edit directly)
- `optimize-state.json` - Optimize-protocol frozen-path list (PROTECTED — written via `shared_state.write_optimize_state`; consulted by `sessions-enforce.is_frozen_path` to block edits to frozen files. Absent on non-optimize projects.)
- `gitlab-mappings.json` - Task-to-GitLab-issue mapping and sync metadata
- `jira-mappings.json` - Task-to-Jira-issue mapping and sync metadata
- `github-mappings.json` - Task-to-GitHub/Gitea-issue mapping and sync metadata

Claude Code configuration in `.claude/settings.json`:
- Hook commands use Windows-style paths with `%CLAUDE_PROJECT_DIR%`
- Python interpreter explicitly specified for `.py` hook execution
- MCP server configuration in `mcpServers` section (optional)

## Key Patterns

### Hook Architecture
- Pre-tool-use hooks for enforcement (sessions-enforce.py)
  - **Corrected execution order** (v0.5.1): Subagent bypass and administrative whitelist now execute before DAIC enforcement
  - Prevents blocking of specialized agents (logging, service-documentation) from editing task files
  - Allows task file creation in discussion mode through administrative whitelist
  - `wiki/` directory unconditionally whitelisted (symlink-safe) for LLM Wiki edits in any mode
- Post-tool-use hooks for reminders and multi-provider auto-sync (post-tool-use.py)
  - Injects the active protocol step's end condition (throttled), monitors auto-compact token usage, and appends auto work-log entries during the implementation step (DAIC mode switching is engine/MCP-driven — the hook emits no mode-switch reminder)
- User message hooks for read-only task/protocol hints and context monitoring (user-messages.py)
- Session start hooks for context loading (session-start.py)
- Shared state management across all hooks (shared_state.py)
- **Windows UTF-8 compatibility** (v0.5.1): All file operations use explicit encoding='utf-8' for markdown and JSONL files (17 locations across 8 files; the two `task-transcript-link.py` transcript-chunk writes were the last stragglers, fixed in h-fix-discard-clean-and-windows-transcript)
- Cross-platform path handling using pathlib.Path throughout
- Windows-specific command prefixing with explicit python interpreter
- Multi-provider API integration with secure token management and rate limiting
- Automated sync hooks for task lifecycle events (status changes, completion)
- Provider-aware routing based on configuration (GitLab, Jira, GitHub / Gitea)
- Structured error handling pattern across all provider utilities (gitlab_utils.py, jira_utils.py, github_utils.py); shared base-class helpers in `issue_provider_base.py` (e.g. `_redact_token`) keep cross-provider logic single-sourced rather than duplicated per provider
- Pagination discipline in list endpoints (`per_page=100` page-loop until a short page) across `gitlab/mr_manager.py::get_mr_notes`, `github/api.py::_get_label_name_to_id_map`, and the branch-finders — avoids silently missing results beyond page 1
- **Hook-command canonicalisation**: module-level `normalize_command()` in `plugin/hooks/hook_utils.py` canonicalises hook commands against quote/separator variations (symmetric per-token quote stripping); consumed by `boot_detector.py` to recognise a legacy pip-install's `.claude/settings.json` hook entries. (The retired installer's `_register_hooks` dedup/heal collapse pass went away with it in #7.) Tests: `test/test_hook_utils.py`

### AI Provider Architecture
- **Provider enablement**: providers are turned on per-phase via the in-session config flow (`/team-management:config` → the `config_update` MCP tool writing `team-management/config.json`); there is no install-time CLI detection or Y/n prompt (that flow was retired with the installer in #7).
- **Runtime Invocation**: Providers run as parallel **Task agents** using dedicated subagent types (`subagent_type: "codex-cli"` / `"agy-cli"`); the system prompt and allowed tools are loaded automatically from the plugin's `agents/<name>.md` definition. Wrapper agents are pass-through (caller owns the full prompt), shell out to `codex` / `agy` CLI in read-only/sandboxed mode with `TIMEOUT_CMD` fallback conditional + `trap` cleanup, and return a standardised review block. No MCP bridge — providers run purely as Task agents.
- **Registry-Driven Dispatch**: `_PHASE_REGISTRY` in `plugin/hooks/ai_providers.py` (re-exported into `protocol_engine.py`) declaratively maps each of the 6 phases to a func, a config flag, a template subpath, and a CLI subcommand (`review` vs `exec`, governing the sandbox-flag check). `_build_handlers` and `get_available_funcs` iterate the registry. Adding a future phase = one row + a 3-line dispatcher method + two markdown templates.
- **Template Lookup**: `_load_provider_template` does 4-tier lookup (custom override → system install → package source → inline default + stderr warning). Templates `.format_map`'d with `_DefaultEmptyDict` for missing-var resilience.
- **Credential Filter**: `_filter_credentials` applies 17 named regex patterns plus a stateful PEM-block pass to task descriptions before provider injection (whole-line redaction with `[REDACTED:<reason>]`). Defense-in-depth for the task-description channel only — the codebase itself is read by the provider's own CLI sandbox.
- **Workflow Hooks**: 6 pre_funcs dispatch through `_resolve_ai_providers_via_registry`: `_func_resolve_ai_providers` (code review — canonical name kept), `_for_brainstorm`, `_for_investigation`, `_for_implementation`, `_for_exploration` (reads `include_in_research_exploration`), `_for_refactoring_planning`. Each reads `enabled_providers` + per-phase `config_flag` from team-management/config.json and emits Task-launch instructions for each enabled provider with the standard `[AI providers: ... participating in <phase>]` discoverability header. **All six phases are fully wired** (m-ai-providers-pilot-and-rollout): JSON `pre_funcs` ✕ sub-protocol Section 0 «Launch AI Providers IN PARALLEL» across `task.json` (investigation, implementation, code-review), `brainstorm.json` (analysis — N+M parallel dispatch with the 6 specialists), `refactoring.json` (planning), `research.json` (exploration). Provider output is treated as advisory; significant findings logged under `## AI Provider Input — <Phase>` work-log sections.
  - Fixed wrapper deadline (codex 300s, agy 330s watchdog); the `ai_providers.timeout` config key is currently inert (not read by the wrappers). Other time budgeting delegated to the surrounding Task agent and providers' internal limits.
  - Graceful degradation: wrapper failures return a `<provider> review unavailable: …` block, never block the workflow. Missing template variables degrade gracefully via `_DefaultEmptyDict` (relevant for cold-start `protocol_start("task")` paths where task file does not yet exist when the resolver fires).
- **Current Providers**: CodexProvider, AgyProvider
- **Future Providers**: Architecture supports adding LocalLLMProvider, etc., by adding a new wrapper agent under `plugin/agents/` + a `_PHASE_REGISTRY` row + a dispatcher method.

### MCP Server Architecture (Modular Design)
- FastMCP-based server implementation exposing 42 MCP tools across 8 tool modules
- **Modular Structure** (refactored from 3,458-line monolith to clean architecture):
  ```
  plugin/mcp/
      server.py              # Entry point (86 lines - imports and registration)
      core/                  # Shared infrastructure
          config.py          # Configuration loading, provider detection
          project.py         # Project root, task file utilities
      helpers/               # Utility functions
          code_review_utils.py # URL parsing, branch validation
      tools/                 # MCP tool implementations (42 tools across 8 modules)
          issue_tracking.py  # 14 issue tracking tools
          protocol.py        # 11 protocol engine tools
          code_review.py     # 4 code review tools
          git_operations.py  # 4 git/MR tools
          daic.py            # 3 DAIC mode switching tools
          notifications.py   # 3 notification tools
          release.py         # 1 release creation tool
          config.py          # 2 config tools
  ```
- **Tool Registration Pattern**: Each module exports `register_tools(mcp)` function
- **Zero-Duplication Import Strategy**: Server imports provider utilities from the plugin's hooks dir, resolved plugin-root-first (`${CLAUDE_PLUGIN_ROOT}/hooks`, with `.claude/hooks` as a legacy fallback) — no file copying
- Dynamic provider detection from team-management/config.json
- Automatic tool registration based on active provider
- Stateless design delegating to existing provider utilities
- Configuration-driven behavior with no code changes to agents
- Security through environment-based project root detection

#### Single Source of Truth Pattern
Both hooks and the MCP server run the same provider utilities from a single location — the MCP server never keeps its own copy. `get_hooks_path()` (`plugin/mcp/core/project.py`) resolves that location plugin-root-first: `${CLAUDE_PLUGIN_ROOT}/hooks` (the plugin install), then `.claude/hooks` (legacy deployed layout), then the dev `plugin/hooks` checkout.
- **Hooks**: import their siblings directly.
- **MCP Server**: prepends the resolved hooks dir to `sys.path`, then imports the same modules.

Importing from one canonical location guarantees hook-driven auto-sync and MCP tool operations execute byte-identical code (no version skew).

### MCP Tool Interface
All team-management functionality is exposed through `mcp__plugin_team-management_tm__*` MCP tools:

- `mcp__plugin_team-management_tm__issue_status` - Check provider status and configuration
- `mcp__plugin_team-management_tm__issue_read` - Import issue as Claude task
- `mcp__plugin_team-management_tm__issue_create` - Create provider issue from task
- `mcp__plugin_team-management_tm__issue_sync` - Sync task status to linked issue
- `mcp__plugin_team-management_tm__issue_link` - Link existing task to issue
- `mcp__plugin_team-management_tm__issue_unlink` - Remove issue link from task

MCP tools are first-class integrated tools in Claude Code's tool system with guaranteed discoverability, structured error handling, and type-safe parameters.

### Agent Delegation
- Heavy file operations delegated to specialized agents
- Agents receive full conversation transcript for context
- Agent results returned to main conversation thread
- Agent state isolated in separate context windows

### Task Structure
- Markdown files with standardized sections (Purpose, Context, Success Criteria, Work Log)
- Directory-based tasks for complex multi-phase work
- File-based tasks for focused single objectives
- Automatic branch mapping from task naming conventions

### Subagent Protection
- Detection mechanism prevents DAIC reminders in subagent contexts
- Subagents blocked from editing .claude/state files
- DAIC subagent bypass covers ALL tools (Bash included) — gated on `in_subagent_context()` alone, so AI-provider wrapper subagents (codex-cli/agy-cli) can shell out via Bash in documentation-mode steps; universal guards (protected-path, frozen-path, manual task archival) run before the bypass so it opens no hole (h-fix-subagent-bash-daic-bypass)
- Strict separation between main thread and agent operations

### Windows Compatibility
- Platform detection using `os.name == 'nt'` (Python) and `process.platform === 'win32'` (Node.js)
- File operations skip Unix permissions on Windows (no chmod calls)
- Command detection handles Windows executable extensions (.exe, .bat, .cmd)
- Global command installation to `%USERPROFILE%\AppData\Local\team-management\bin`
- Hook commands use explicit `python` prefix and Windows environment variable format

## Package Structure

### Distribution
- Single Claude Code plugin under `plugin/` (pip/pipx/npm packaging retired in #7)
- Marketplace install via `.claude-plugin/marketplace.json`, or local `--plugin-dir ./plugin`
- Cross-platform compatibility (macOS, Linux, Windows 10/11)

### Template System
- Task templates for consistent structure
- `CLAUDE.tm.md` plugin-owned behavioral template (replaced on every update) plus `CLAUDE.tm.custom.md` user-owned stub (created once, never overwritten). In plugin mode the behavioral guidance is delivered via native `@`-includes in the project-root `CLAUDE.md`: `session-start.py` (and the `config_update` MCP tool) deploy the plugin-owned files into the project, THEN wire an idempotent `<!-- team-management:begin … -->`/`<!-- team-management:end -->` managed block that `@`-imports `CLAUDE.tm.md` + `CLAUDE.tm.custom.md` (+ `CLAUDE.wiki.md` when wiki is enabled) — `shared_state.ensure_guidance_deployed_and_wired` (deploy-before-wire). This replaces the former SessionStart `additionalContext` injection, which faded over long / `/compact`-ed sessions; a project-root `CLAUDE.md` and its `@`-imports are re-read after `/compact`, so the guidance is durable (h-durable-guidance-via-claude-md). (The retired installer's CLAUDE.md wiring + upgrade migration went away with it in #7.)
- Protocol markdown files for complex workflows
- Agent prompt templates for specialized operations

## Quality Assurance Features

### Context Management
- Token counting and usage warnings at 80%/90% thresholds
- Automatic context compaction protocols
- State preservation across session boundaries
- Clean task file maintenance through logging agent

### Work Quality
- Mandatory discussion before implementation
- Code review agent for quality checks
- Pattern consistency through context gathering
- Branch enforcement prevents wrong-branch commits

### Process Integrity
- Hook-based enforcement cannot be bypassed
- State file protection from unauthorized changes
- Chronological work log maintenance
- Task scope enforcement through structured protocols

## Related Documentation

- docs/INSTALL.md - Detailed installation guide
- docs/USAGE_GUIDE.md - Workflow and feature documentation
- plugin/knowledge/ - Internal architecture documentation
- README.md - Marketing-focused feature overview
- team-management/protocol-configs/ - Workflow protocol specifications (JSON engine configs + sub-protocols)

## team-management Behaviors

Behavioral guidance (DAIC, TDD, debugging, code-review discipline) and wiki behavior are plugin-owned. When the plugin is active, `session-start.py` (and the `config_update` MCP tool) deploy `plugin/templates/CLAUDE.tm.md` / `CLAUDE.wiki.md` into the project and wire a managed `<!-- team-management:begin … -->`/`<!-- team-management:end -->` block into the project-root `CLAUDE.md` that `@`-imports them — durable across `/compact` (the former SessionStart `additionalContext` injection faded over long sessions and was removed in h-durable-guidance-via-claude-md). Project-specific rules go in `CLAUDE.tm.custom.md` (created by `/team-management:init`; untracked in this repo).

<!-- team-management:begin (managed by /team-management:init; do not edit inside) -->
@CLAUDE.tm.md
@CLAUDE.tm.custom.md
@CLAUDE.wiki.md
<!-- team-management:end -->
