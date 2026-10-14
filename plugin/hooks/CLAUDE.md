# Hooks Module CLAUDE.md

## Purpose
Behavioral enforcement (DAIC, branches, protected paths, safety guards), shared state, the protocol engine, and the issue-tracking provider utilities. This directory is the single source of truth for provider code: hooks import it directly, the MCP server puts it on `sys.path`.

## How Hooks Run
- `hooks.json` registers every hook as `python3 ${CLAUDE_PLUGIN_ROOT}/hooks/_shim.py <hook-file>` (8 commands). SessionStart matcher: `startup|clear|resume`. PreToolUse matchers: `Write|Edit|MultiEdit|NotebookEdit|Task|Bash|Read|Grep` → `sessions-enforce.py`; `Task|Agent` → `task-transcript-link.py`.
- `NotebookEdit` must stay in the sessions-enforce matcher — the hook special-cases it in every write gate (drift-guarded by `test/test_hooks_matcher_drift.py`).
- Keep the literal `python3` token. macOS has no real `python`; Windows gets a `python3.exe` copy provisioned by `/team-management:init`.
- `_shim.py` — stdlib launcher: venv python under `${CLAUDE_PLUGIN_DATA}/venv`, else `sys.executable`, else a system python. A missing hook file exits 0 with a stderr note (a manifest typo must never hard-block tools).
- Hook files that read stdin at import (`sessions-enforce.py`, `user-messages.py`, `session-start.py`, `post-tool-use.py`) are NOT importable by tests. Put testable logic in an importable sibling (`hook_utils.py`, `update_notice.py`, `shared_state.py`) or drive the hook as a subprocess (`test/test_user_messages_hook.py` pattern).
- `is_plugin_mode()` (`shared_state.py`) = `CLAUDE_PLUGIN_ROOT` is set. It gates every plugin-only behaviour so this dev checkout never self-blocks.

## Hook Files
- `sessions-enforce.py` — PreToolUse enforcement. Order inside `main()`: universal guards (protected paths, frozen paths, manual-archival block, safety guard) → read-only fast exit → subagent bypass → admin whitelist → DAIC mode → branch enforcement. Universal guards run before the subagent bypass, so subagents are bound by them.
- `session-start.py` — task context loading, DAIC restore, guidance deploy+wire, statusline pin, agy gate deploy, provider-tokens seeding, update notice, migration warnings. Reads the SessionStart payload once; `compact-pending.flag` is kept on `source == "resume"`.
- `user-messages.py` — read-only hints: subagent-depth reset, post-compact restoration, token warnings / auto-compact directive, protocol suggestions, investigation-step auto save-note, whole-word `STOP`/`SILENCE`. Never switches DAIC mode.
- `post-tool-use.py` — end-condition injection (throttled), auto-compact monitoring, auto work-log appends during implementation, provider auto-sync, subagent-depth decrement and transcript archiving.
- `pre-compact.py` — saves the task/branch/protocol/DAIC checkpoint. Must never exit non-zero: any non-zero PreCompact exit aborts `/compact`.
- `task-transcript-link.py` — on `Task`/`Agent` PreToolUse: increments subagent depth, stages the last 1 MB of the parent transcript into `.claude/state/{subagent_type}/{key}/`.
- `config_intent_gate.py` — writes `.claude/state/config-session.flag` when the user physically runs `/team-management:config` or `/team-management:init` (UserPromptExpansion structural match + UserPromptSubmit first-token match). Always exits 0.
- `boot_detector.py` — `detect_legacy_install(project_root)`: a legacy `.claude/hooks/` dir plus a corroborating settings/MCP signal. SessionStart advises; `sessions-enforce.py` blocks edits.
- `hook_utils.py` — `normalize_command`, `command_is_read_only` (whole-token match: `cdk deploy` is not `cd`), `is_task_markdown_path` (slash-normalized).
- `update_notice.py` — plugin-update notice logic: semver parse, changelog section extraction, missed-section selection, persistent marker, `format_banner` (ASCII, user-facing `systemMessage`) and `format_notice` (`additionalContext`).
- `claude_md_hygiene.py` — content rules for CLAUDE.md lines (task names, history wording, review chatter) behind the `check_claude_md_hygiene` post_func.

## Subagent Context
- `.claude/state/subagent-depth.json` is a locked counter, not a flag: `task-transcript-link.py` increments on `Task`/`Agent` PreToolUse, `post-tool-use.py` decrements on PostToolUse (clamped at 0). Both recognise BOTH tool names — some harnesses call the dispatch tool `Agent`.
- `in_subagent_context()` (depth > 0) gates the DAIC subagent bypass (all tools, Bash included), reminder/auto-sync suppression, and the auto-worklog.
- Reset to 0 on every UserPromptSubmit and SessionStart, so a hard-interrupted Task cannot leak.
- In `task-transcript-link.py` every failure after the increment decrements and exits 2 (blocking the Task), so `post-tool-use.py` never double-decrements.

## Enforcement Details (sessions-enforce.py)
- Protected paths: `.claude/state` prefix (state files, protocol logs, provider tokens, confirmation nonces), plus `get_plugin_root()/protocol-configs` when the plugin lives outside the project. `check_bash_protected` is substring matching on purpose. A friction guard, not a security control: `.claude/./state` spellings bypass it.
- Documentation mode allows any `.md` file, anything under `team-management/tasks/`, `/docs/` paths and `wiki/`; it blocks other source edits.
- Branch enforcement resolves relative paths before walking to the git repo.
- Failure model: foreseeable state corruption fails closed (restrictive reader defaults); unforeseen exceptions fail open loudly (`main()` wrapper prints and exits 0).
- Frozen paths (optimize protocols): `_load_frozen_paths` returns `[]` unless `optimize-state.json` exists AND the active step is `experimentation`; `is_frozen_path` and `_bash_targets_frozen` match on path-component boundaries. Bash target extraction is regex-based, not a shell parser.

### Safety Guards (`check_safety_guard`)
- Friction, not security: the model can run both steps itself.
- Blocks: `rm` with recursive+force flags, SQL `DROP TABLE|DATABASE`/`TRUNCATE`, force pushes, `git reset --hard`, `git checkout|restore .`, `git clean -f` without dry-run, `kubectl delete`, `docker rm -f` / `docker system prune`.
- Segment-aware parsing after wrapper stripping (`_WRAPPER_COMMANDS`); an unparseable segment degrades to a whitespace-token scan plus the SQL regex.
- Confirmation: exit 2 with a nonce stored in `.claude/state/safety-guard-confirm.json`; the re-run is `SAFETY_CONFIRM=<nonce> <exact command>`. Exact raw-command binding, TTL 600s, single use, last block wins.
- Allowlist: anchored whole-command regex for `rm -rf` of build/cache dirs, plus `safety_guards.allowlist` exact strings.
- Own-branch exception (`warn-destructive-only` only): `git reset --hard` and force pushes limited to the task branch, when the branch has no remote-tracking ref. Any git failure, `cd`, `-C`, `--git-dir` or broad push mode denies.
- Config `safety_guards.{enabled,sensitivity,allowlist}`; malformed values fall back to defaults (enabled). Workflow bypass disables the guard.
- Known bypasses (variables, `bash -c`, `find -exec`, base64 pipes, a cwd from an earlier Bash call) are listed in `docs/USAGE_GUIDE.md`.
- Tests: `test/test_safety_guards.py`.

## Shared State (shared_state.py)
- Three roots, each resolved at the point of use: `get_project_root()` (`CLAUDE_PROJECT_DIR` first), `get_plugin_root()` (read-only install; `CLAUDE_PLUGIN_ROOT`, dev fallback `<repo>/plugin`), `get_plugin_data()` (`CLAUDE_PLUGIN_DATA` only, `None` when unset — persistent state must never live under the plugin root). There is no module-level `PLUGIN_ROOT` constant: an import-time value would ignore later env changes.
- Writers: `_write_json_durable` (unique `mkstemp` temp + fsync + `os.replace`). Integrity writers (`set_daic_mode`, `set_task_state`, `set_protocol_state`, `edit_state`) take `_state_lock` and raise on lock failure; best-effort consumers (counters, subagent depth) degrade instead.
- `increment_counter_file` / `reset_counter_file` — locked int counters for the PostToolUse throttles. `set_protocol_state` / `clear_protocol_state` delete the end-condition counter so the next tool call re-injects the step's end condition.
- `set_protocol_state(..., extra=...)` merges loop state (`loop_iteration`, `experimentation_started_at`) into the protocol block.
- `load_protocol_config` / `resolve_protocol_start_text` search custom → plugin root → legacy deployed system → project bases; `@sub-protocols/...` and `@knowledge/...` refs resolve in dev, installed and plugin layouts. Only the top-level `@` ref is expanded.
- `parse_task_frontmatter` — the single frontmatter parser. `read_last_jsonl_entry` / `_read_file_tail` — bounded tail reads for transcripts that grow to tens of MB.
- `resolve_provider_token(provider)` — reads `.claude/state/provider-tokens.json` (per project, `0600`, user-authored), falling back to `config.json`. `ensure_provider_tokens_file()` seeds the file only when absent.
- Config-session gate: `write_config_session_flag` / `check_config_session_flag` (TTL 900s); the flag path resolves at call time.
- Guidance delivery: `ensure_guidance_deployed_and_wired(project_root, plugin_root, wiki_enabled)` deploys `CLAUDE.tm.md`, the knowledge files and (wiki on) `CLAUDE.wiki.md`, creates `CLAUDE.tm.custom.md` if absent, then writes the `<!-- team-management:begin … -->` managed block of `@`-imports into the project `CLAUDE.md`. Deploy always precedes wire. `ensure_task_template_deployed` creates `team-management/tasks/TEMPLATE.md` if absent (atomic byte copy).
- agy containment: `ensure_agy_readonly_gate_deployed` (gate script + merge-aware `.agents/hooks.json` entry, gitignores `.agents/`), `agy_gate_is_deployed` (deep-equal check for the wrapper preflight), `ensure_agy_permission_rules` (union-merge into the user's global `~/.gemini/antigravity-cli/settings.json`; called ONLY from `config_update`, with consent), `agy_permission_rules_present`. `_AGY_PERMISSION_RULES` grants git per subcommand, never bare `command(git)`, and must stay a superset of the gate's base commands (`test/test_agy_permission_rules.py`).
- `get_provider_logger(name)` — appends to `<project>/.claude/logs/<name>`.
- `task_state_manager.py` — `.claude/state/tasks/<task>/` paths; raises on an empty task name so cleanup can never resolve to the parent directory.

## Protocol Engine (protocol_engine.py + mixins)
`class ProtocolEngine(RetroMixin, OptimizeCompletionMixin, AIProvidersMixin)`. The core file holds step lifecycle (start/advance/goto/abort/resume), audit logging, git/task funcs, the code-review gates, looping steps, and `_build_handlers` / `get_available_funcs`.
- Every handler needs a metadata entry in `get_available_funcs` and vice versa; `test/test_completion_workflow.py` fails on a mismatch.
- Patch invariants: `protocol_engine.py` keeps `import subprocess` / `import os`, and mixins call `subprocess.run(...)` qualified, so `patch("protocol_engine.subprocess.run")` reaches mixin code (`test/test_engine_split_patch_invariant.py`).
- `advance_step` injects `advance_summary` into post_func args; post_funcs block only when the step sets `post_funcs_stop_on_failure: true`; pre_funcs never block.
- Logs: `_pending.json` until `set_task_state` renames it to `<task>.json`. Review gates read ONLY the task's own log and fail closed when it is missing.
- `abort_protocol` clears protocol state, wipes `.claude/state/tasks/<task>/`, nulls task identity, and returns DAIC to discussion. It keeps the git branch, the task file and `optimize-state.json`.

### Task creation
- `_validate_task_file_structure` — the single validator for inline and write-file-first delivery: frontmatter, `status`, branch match, priority prefix, `## Success Criteria`, no unresolved `[NEEDS CLARIFICATION` marker.
- `_func_create_task_file` with empty `task_content` re-validates the file on disk and never mutates it; a missing `**Author:**` only warns.

### Code-review step gates
- `classify_review_dispatch` (pre_func, task + refactoring) — measures `git diff HEAD --numstat` plus untracked lines, excluding `team-management/`, `.claude/`, `.agents/` (`_FRAMEWORK_PATH_ROOTS`). testing-specialist / code-cleanliness fire at ≥50 lines (never on docs/config-only diffs), red-team above 200. Never blocks; per-agent JSONL telemetry goes to `.claude/state/review-dispatch-log.jsonl`. `wiki/` and `docs/` count as project content.
- Review tree hash — `_compute_review_tree_hash()`: sha256 over length-prefixed records of `HEAD`, the tracked diff (`--binary --no-ext-diff --no-textconv`), and every untracked file, framework roots excluded. Work-log edits never change it; any code edit or commit does. Raises on git failure or a repo with no HEAD.
- `save_note` stamps `tree_hash` on `SPEC_REVIEW: PASSED`, `SPEC_DEFERRED: PASSED` and `REVIEW_ROUND: …` notes (matched on the stripped text). A hash failure stores `tree_hash: null` + `tree_hash_error` instead of raising.
- `verify_review_round` (first post_func) — the latest `REVIEW_ROUND:` note must match the current tree. Missing note, null hash or a hash failure blocks.
- `require_spec_review_passed` (post_func, after the round check) — the latest sentinel saved in an audit step must match the current tree (an unreadable protocol config blocks), and a `SPEC_DEFERRED:` carriage must exist at or after it (`_deferral_without_gate`; recovery `SPEC_DEFERRED: none`); a carriage listing SC-IDs blocks unless a later step of the active protocol runs `require_deferred_criteria_done`. An unreadable protocol state or config counts as no such step. A sentinel without a `tree_hash` key (written before hashing existed) falls back to the timestamp check against backward gotos to `_CODE_CHANGING_STEPS`.
- `verify_tests_pass` — runs `test_command` with `shell=False`; metacharacters are rejected on the raw string before `shlex.split`, then the prefix allowlist applies. Absent from refactoring on purpose (baseline failures are tolerated there).
- `check_completion_evidence` — the advance summary must carry verification evidence or `no-verification-applicable: <reason>`.
- `validate_code_review_in_worklog` / `validate_no_critical_issues_in_worklog` — parse the LAST `# Code Review:` block for `## 🔴 Critical Issues (N)` (emoji optional).

### Documentation step
- `check_claude_md_hygiene` (post_func on task documentation and optimize synthesis) — judges only lines ADDED to any `CLAUDE.md` (tracked diff plus untracked files): blocks on task names from `team-management/tasks/`, history wording, review chatter, and on a file that grew past `documentation.claude_md_max_lines` (default 300). Growth and the budget count non-blank lines, excluding marker lines and `@`-imports inside the managed block (`count_content_lines`), so session-start wiring never counts against a task and no marker arrangement can hide content. `documentation.claude_md_hygiene: false` disables it; a git failure passes with a warning.
- Step ownership (`_steps_running`, `_notes_of_steps`, `_audit_notes`): `SPEC_REVIEW: PASSED` and `SPEC_DEFERRED:` carriages count only when saved in a step that runs `require_spec_review_passed`, and `SPEC_DEFERRED: PASSED` only in a step that runs `require_deferred_criteria_done`, both read from the active protocol config. A note saved in any other step can neither satisfy nor cancel a gate. Other steps' notes are blanked, not dropped, so log positions stay real for the (timestamp, position) tie-break.
- `require_deferred_criteria_done` (post_func on the task documentation step) — reads the latest audit-step carriage (never `SPEC_DEFERRED: PASSED` itself); an unreadable protocol config blocks. No carriage or `none` passes; a carriage older than the latest `SPEC_REVIEW: PASSED` blocks; listed SC-IDs need a newer `SPEC_DEFERRED: PASSED` whose `tree_hash` matches the current tree. Notes are ordered by (timestamp, log position); a missing log fails closed.

### Completion
- `present_completion_options` / `require_discard_confirmation` / `completion_dispatch`. Provider `disabled` (or no provider key with no provider enabled) → menu `merge_local` / `push_pr` / `keep` / `discard`; otherwise the provider chain (archive → commit → merge main → push → MR/PR → issue status → cleanup → checkout default).
- Local flows require HEAD to be the task branch. Default branch: `origin/HEAD` → probe `origin/main|master` → `main`.
- `discard` needs `discard_confirmation="discard"` plus `discard_confirmed_dry_run=true` (friction, not security). Its `git clean -fdx` excludes `team-management/` and `.claude/`.
- `git_merge_main` / `git_push` skip when there is no `origin` remote. `push_pr` checks `gh pr view` before `gh pr create`.
- `create_merge_request` reports `created | skipped | failed`.

### Looping steps (optimize)
- `looping_step: true` re-runs the step's pre_funcs instead of advancing; exit via `exit_loop` or a post_func returning `terminate`.
- `restart=true` archives `results.tsv` to `results.tsv.run-N` and resets the loop, keeping the baseline.
- `start_protocol` resumes an active looping protocol after scanning recent TSV rows and the stdout tail for credentials; `resume_force_safe` bypasses the scan (audit-logged).

## Mixin Modules
- `engine_constants.py` — `GIT_TIMEOUT_FAST|MEDIUM|SLOW`, `TEST_TIMEOUT`.
- `ai_providers.py` — `AIProvidersMixin` and the module globals:
  - `_PHASE_REGISTRY` — the six phases; each row has func, config flag, template path, subcommand.
  - `_read_provider_template` (custom → plugin → legacy deployed → inline default) and `_render_provider_template` (`format_map` with `_DefaultEmptyDict`).
  - `_filter_credentials` (17 patterns + PEM pass), then `_fence_untrusted` (nonce-tagged markers).
  - `_ensure_sandbox_flags` raises `SandboxFlagError` and checks the RAW template, never the rendered prompt.
  - `_resolve_ai_providers` re-raises only `AssertionError` / `SandboxFlagError`; every other failure degrades gracefully.
- `optimize_completion.py` — `OptimizeCompletionMixin`:
  - metric runs: filtered env, `shell=False`;
  - `_bounded_regex_search`: the user's `metric_parser` runs in a killable child process, 5s timeout, 256K-char cap;
  - `log_experiment_result`: always measures itself; dirty-tree gate; TSV schema `iteration timestamp commit_sha metric_value run_count aggregator wall_clock_s status hypothesis`;
  - `check_termination`, `update_best_commit`, `batch_checkpoint` (fires on `(loop_iteration + 1) % batch_size == 0`), `policy_compliance_audit` (never blocks);
  - `_completion_optimize` + `_squash_to_best`: aborts before any reset when HEAD cannot be captured; rolls back on a partial failure;
  - the unified completion dispatcher.
- `retro.py` — `RetroMixin`:
  - `_read_protocol_logs_typed(since)` returns engine-derived fields only, never free text — this guards against cross-task prompt injection, and counts each event by its own timestamp inside the git harvest's window;
  - `_retro_guard` reads only the local default branch (no remote needed). It blocks on stale or scarce data (window-recoverable: `_WINDOW_DEPENDENT_PREFIXES`) and on a detached HEAD or any git failure (abort-only);
  - `_retro_harvest_git` uses NUL separators and is sanitized + credential-filtered;
  - the four retro funcs.

## Provider Utilities (single source of truth)
- `issue_provider_base.py` — `IssueTrackingProvider` base:
  - `_find_project_root` reads the env first;
  - `_redact_token` runs before the 500-char error-body truncation;
  - `_warn_if_insecure_base_url` warns on non-https;
  - shared request / error / slug / title helpers;
  - code-review block extraction for MR/PR comments takes the last `# Code Review:` block, up to the next single-`#` heading.
- `IssueTrackingTaskSync` — mapping files `.claude/state/<provider>-mappings.json`. Read-modify-write runs under a lock. A missing file loads `{}`; a corrupt one is copied to `.corrupt-<ts>` and raises.
- `gitlab_utils.py`, `github_utils.py`, `jira_utils.py` — backward-compatible facades over the `gitlab/`, `github/` (GitHub + Gitea, detected from `base_url`) and Jira modules.
- List endpoints page with `per_page=100` until a short page.
- `git_operations.py` — `validate_branch_name` and the shared git helpers.
- Optional imports: `requests` / `tiktoken` fall back to `None`, so hooks run before the venv exists. Provider `_make_request` returns a `dependency_missing` error; token counting falls back to a character estimate.
- Every file read and write passes `encoding="utf-8"` — Windows defaults to cp1252.

## Tests
Hook behaviour is covered by subprocess harnesses in `test/` (`test_user_messages_hook.py`, `test_safety_guards.py`, `test_post_tool_use_lock_resilience.py`, `test_session_start_*.py`). Engine coverage: `test_protocol_engine*.py`, `test_completion_workflow.py`, `test_review_dispatch_gate.py`, `test_review_freshness_gates.py`, `test_claude_md_hygiene.py`, `test_retro_protocol.py`.
