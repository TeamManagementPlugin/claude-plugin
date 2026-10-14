# Plugin CLAUDE.md

## Purpose
The team-management Claude Code plugin: hooks that enforce the DAIC workflow, a JSON-driven protocol engine, an MCP server, subagents, slash commands, and the templates and knowledge it deploys into a project. Everything under `plugin/` ships as the plugin install (`${CLAUDE_PLUGIN_ROOT}`), which is read-only and replaced on every update.

Module detail lives next to the code: `hooks/CLAUDE.md`, `mcp/CLAUDE.md`, `agents/CLAUDE.md`.

## Layout
- `.claude-plugin/plugin.json` — manifest. Its version must match `.claude-plugin/marketplace.json` and have a `## [<version>]` section in `CHANGELOG.md` (release CI hard-fails otherwise). It declares no `userConfig`.
- `hooks/hooks.json` — hook registrations through `hooks/_shim.py`.
- `.mcp.json` — MCP server `tm` → `mcp/bootstrap_mcp.py`.
- `hooks/` — hook scripts, `shared_state.py`, the protocol engine and mixins, provider utilities.
- `mcp/` — the MCP server (42 tools across 8 modules).
- `agents/` — subagent definitions.
- `commands/` — slash commands, namespaced `/team-management:<file name>`:
  - `config`, `init`, `changelog`, `clean-check`;
  - `wiki-ingest`, `wiki-lint`, `wiki-tune`;
  - `custom-protocol-create`, `custom-protocol-update-after-reinstall`.
  - File names carry no `tm-` prefix (`test/test_command_namespace.py`).
- `protocol-configs/` — protocol JSONs plus:
  - `sub-protocols/` — step text loaded through `@sub-protocols/<file>.md`;
  - `providers/` — `<codex|agy>-<phase>.md` AI-provider prompt templates;
  - `funcs/` — an example custom func.
- `templates/` — files deployed into projects:
  - `CLAUDE.tm.md` — behavioural guidance; byte-identical to the repo-root copy (`test/test_behavior_rules_drift.py`);
  - `CLAUDE.tm.custom.md`, `CLAUDE.wiki.md`, `wiki-schema.md`;
  - `TEMPLATE.md` — task template;
  - `config.template.json`, `statusline.py`;
  - `agy-readonly-gate.py`.
- `knowledge/` — `tdd-discipline.md`, `debugging.md`, `receiving-feedback.md` (deployed to `team-management/knowledge/`, read on demand) and `claude-code/` reference notes.
- `scripts/recover_tsv.py` — restores an optimize task's `results.tsv` from `.results.tsv.bak`.
- `requirements.in` / `requirements.lock` — runtime deps (`mcp`, `tiktoken`, `requests`). The lock is universal and hashed (`uv pip compile --generate-hashes --universal`).

## Three Roots
- Project (`CLAUDE_PROJECT_DIR`) — user files, `team-management/`, `.claude/state/`.
- Plugin root (`CLAUDE_PLUGIN_ROOT`) — read-only install.
- Plugin data (`CLAUDE_PLUGIN_DATA`) — the venv and persistent markers such as `tm-last-seen-version.json`.
- Resolve each with the `shared_state.get_*` helpers at the point of use. Never persist state under the plugin root.

## Project Footprint
- `team-management/`:
  - `config.json` — gitignored, `0600`;
  - `tasks/` (`done/` for archived tasks);
  - `protocol-configs/custom/`;
  - `knowledge/`, `statusline.py`.
- `.claude/state/` (protected):
  - `current_task.json`, `daic-mode.json`;
  - `protocol-logs/`, `tasks/<task>/`;
  - `<provider>-mappings.json`, `provider-tokens.json`;
  - `optimize-state.json`, `review-dispatch-log.jsonl`, `safety-guard-confirm.json`, `subagent-depth.json`.
- Project-root `CLAUDE.md` gets a managed `<!-- team-management:begin … -->` block of `@`-imports (`CLAUDE.tm.md`, `CLAUDE.tm.custom.md`, `CLAUDE.wiki.md` when the wiki is on). The imports are re-read after `/compact`.
- `CLAUDE.tm.md` cites knowledge files as plain backticked paths, NOT `@`-imports — imports would load the whole knowledge tree into every session.
- `.claude/settings.local.json` pins the statusLine to the resolved absolute `templates/statusline.py` with `sys.executable`. Claude Code does not expand `${CLAUDE_PLUGIN_ROOT}` in settings, so the pin must be absolute.
- `.agents/` (gitignored) — the agy read-only gate, deployed when agy is enabled.

## Protocols
Configs in `protocol-configs/*.json`; the search order is custom (`team-management/protocol-configs/custom/`) → plugin → legacy deployed system.
- `task` — investigation → implementation → code-review → documentation → completion.
- `brainstorm` — topic → discussion → analysis (6 specialists + providers) → results → planning.
- `research` — scoping → exploration → synthesis → conclusion.
- `refactoring` — test-baseline → planning → refactoring → test-verify → code-review → completion. Omits `verify_tests_pass` (baseline failures are tolerated).
- `optimize` / `optimize-unattended` — setup → metric-script → baseline → experimentation (looping) → synthesis → code-review → completion.
  - The two files differ only in the experimentation step (batched checkpoints vs. unattended); `test/test_optimize_json_drift_guard.py` keeps the shared steps identical.
- `retro` — data-collection → metrics → narrative → snapshot. Branchless `r-retro-<date>` task, no commits; the report goes to `docs/retros/`.
- Every step `end` is a `- [ ]` checklist (`test/test_protocol_end_checklist.py`).
- Custom funcs: `team-management/protocol-configs/custom/funcs/*.py`, referenced as `custom(func_name)`.
- `protocol_customize` forks a system protocol into `custom/` with a `.forked-from.json` sidecar; `protocol_check_drift` reconciles the fork after an update.

### Code-review step
Rounds first, spec audit last (`sub-protocols/code-review.md`):
1. Dispatch `code-review` + providers + classifier-fired specialists in one parallel message; red-team as the second echelon.
2. Confirm each finding in the code. Route blocking findings by fix depth:
   - SIMPLE → fix in step (the covering test is not a second file; a changed default, flag, branch or condition is COMPLEX);
   - COMPLEX → back to the protocol's code-writing step (`implementation`, `refactoring`, `experimentation`);
   - AMBIGUOUS → back to its planning step (`investigation`, `planning`), or a direct question in optimize.
3. Record `REVIEW_ROUND: …` after every round.
4. Run the spec audit once on the final tree and record `SPEC_REVIEW: PASSED` + `SPEC_UNVERIFIABLE: …` + `SPEC_DEFERRED: …`. The `SPEC_DEFERRED:` carriage is required. In `task` a documentation criterion (`CLAUDE.md`, `README*.md`, `CHANGELOG.md`, `wiki/`, `docs/`) not written yet is `DEFERRED` to the documentation step; refactoring and optimize defer nothing.

Post_funcs (task): `verify_review_round`, `require_spec_review_passed`, `verify_tests_pass`, `check_completion_evidence`, `validate_code_review_in_worklog`, `validate_no_critical_issues_in_worklog`.
- The two note gates compare an engine-stamped working-tree hash.
- In strict mode (`code_review.enforce_warnings`) a confirmed Warning blocks; Notes never block.

### Documentation step
CLAUDE.md files are a lean reference to the current code. `check_claude_md_hygiene` blocks added lines that name tasks, use history wording or carry review chatter, and growth past `documentation.claude_md_max_lines` (default 300).
`require_deferred_criteria_done` blocks until every deferred documentation criterion is re-audited and confirmed by a `SPEC_DEFERRED: PASSED` note that matches the current tree.

### Completion
`completion_dispatch`:
- provider-driven chain for GitLab / GitHub / Jira;
- for `issue_tracking.provider: "disabled"` (also inferred when no provider is configured), a menu `merge_local` / `push_pr` / `keep` / `discard`.
- `discard` requires a two-step typed confirmation, and its `git clean` spares `team-management/` and `.claude/`.

## Configuration Flow
- `/team-management:config` drives `config_get` / `config_update`. Writes require the session flag that only `config_intent_gate.py` can create.
- Non-secret keys are validated against `_CONFIG_SCHEMA` (`mcp/tools/config.py`). Every key needs a description.
- Provider tokens live in the per-project, user-authored `.claude/state/provider-tokens.json`, never in `config_update`.
- `/team-management:init` merges plugin enablement into the project `.claude/settings.json` and offers the wiki. On Windows it provisions `python3.exe`.

## AI Providers
- Codex and agy run as parallel Task agents (`agents/codex-cli.md`, `agents/agy-cli.md`). They are enabled per phase by `ai_providers.include_in_<phase>`, and agy also needs `agy.enabled`.
- Steps that dispatch providers and keep working (task investigation and implementation, refactoring planning, research exploration) carry a `**Wait for every provider.**` barrier in §0. It allows only read-only repository work until every provider returns, and names the step's gated action. Drift-guarded by `test/test_provider_wait_barrier.py`.
- `_PHASE_REGISTRY` (`hooks/ai_providers.py`) defines the six phases: `code_review`, `brainstorm`, `investigation`, `implementation`, `research_exploration`, `refactoring_planning`.
- Prompts come from `protocol-configs/providers/<provider>-<phase>.md`. Task text is credential-filtered and fenced as untrusted before it is injected.
- The sandbox-flag check runs on the RAW template:
  - codex `exec` must carry `-s read-only`;
  - codex `review` must carry no scope flag;
  - agy must carry neither `--sandbox` nor `--dangerously-skip-permissions`.
- agy containment = the project `.agents/hooks.json` read-only deny-gate plus consent-written `permissions.allow` rules in the user's global `~/.gemini/antigravity-cli/settings.json`.
  - A gate limitation: config-driven `git diff` drivers (`diff.external`, textconv) run inside git and cannot be intercepted, so do not point agy at an untrusted repo's `.git/config`.
  - agy reads only the repository: a prompt naming a path outside it is refused before launch, so pass such content (severity scale, `## Previous rounds`) inline.
- Wrappers enforce fixed deadlines (codex 300s, agy 480s print-timeout under a 510s watchdog). `ai_providers.timeout` is not read.
- Every `agy-<phase>.md` template carries a `## Budget` section: gate-denied commands to avoid, read only the needed line ranges, start answering early (a mid-turn deadline loses the whole answer).
- A failure returns `<provider> review unavailable: …` and never blocks the workflow.
- Legacy keys (`include_in_architecture`, `include_in_exploration`, `gemini.*`) only trigger a one-time session-start warning. Their values are never forwarded.

## Release Notes
- `CHANGELOG.md` (Keep a Changelog, newest first) drives the session-start update notice and `/team-management:changelog`.
- New entries go in the section of the version being prepared; there is no `[Unreleased]` section.
