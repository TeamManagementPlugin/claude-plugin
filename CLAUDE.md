# team-management CLAUDE.md

## Purpose
A Claude Code plugin that enforces the Discussion-Alignment-Implementation-Check (DAIC) workflow for AI pair programming:
- hooks block edits until a workflow protocol puts the session in implementation mode;
- JSON protocols drive every task from investigation to merge;
- an MCP server exposes the engine, issue tracking and git operations as tools;
- specialised subagents review, document and analyse in their own context windows.

## Where Things Are
- `plugin/` — the plugin itself (the only distribution channel). Start with `plugin/CLAUDE.md`.
  - `plugin/hooks/CLAUDE.md` — hooks, shared state, protocol engine, provider utilities.
  - `plugin/mcp/CLAUDE.md` — MCP server and its 42 tools.
  - `plugin/agents/CLAUDE.md` — subagents and the review output contract.
- `.claude-plugin/marketplace.json` — marketplace descriptor pointing at `./plugin`.
- `test/` — pytest suite; many tests are drift guards that pin text across docs, configs and code.
- `docs/` — user docs: `INSTALL.md`, `USAGE_GUIDE.md`, `MCP_SERVER.md`. `docs/knowledge/writing-behavioral-content.md` is the maintainer guide for knowledge files.
- `wiki/` — the project's LLM wiki on the framework's internals (`wiki/index.md` first).
- `.github/` — CI: tests, release integrity (`scripts/check_release_integrity.py`), release notes from the changelog (`scripts/extract_changelog_notes.py`).
- `CLAUDE.tm.md` (repo root) — must stay byte-identical to `plugin/templates/CLAUDE.tm.md`.
- `team-management/` — this repo's own task files and config (gitignored).

MCP server tree (the tool count per module is drift-guarded by `test/test_mcp_tool_inventory.py`):
```
plugin/mcp/tools/
    issue_tracking.py  # 14 issue tracking tools
    protocol.py        # 11 protocol engine tools
    code_review.py     # 4 code review tools
    git_operations.py  # 4 git/MR tools
    daic.py            # 3 DAIC mode switching tools
    notifications.py   # 3 notification tools
    release.py         # 1 release creation tool
    config.py          # 2 config tools
```

## Developing
- Run the plugin from the checkout: `claude --plugin-dir ./plugin`. Configure in-session with `/team-management:init`, then `/team-management:config`.
- Tests: `python3 -m pytest test/ -q`. Stale `__pycache__` from mutation experiments can produce false failures — clear it before chasing a phantom.
- Python 3.10+. Every file read and write passes `encoding="utf-8"` (Windows).
- Hook manifests use the literal `python3` token on purpose. Windows gets a `python3.exe` from `/team-management:init`.
- Release notes go in the `## [X.Y.Z] - YYYY-MM-DD` section of the version being prepared in `plugin/CHANGELOG.md`; there is no `[Unreleased]` section. The version in `plugin/.claude-plugin/plugin.json` needs a matching section, and the marketplace version must match.
- Repository files are written in English.

## Core Concepts
- **DAIC modes**:
  - discussion — edit tools blocked;
  - implementation — full access;
  - documentation — docs, task files, CLAUDE.md and wiki only.
  The protocol engine sets the mode per step; the `daic_mode_switch_*` MCP tools are a manual override.
- **Protocols**: `task`, `brainstorm`, `research`, `refactoring`, `optimize`, `optimize-unattended`, `retro`. Custom protocols in `team-management/protocol-configs/custom/` override the shipped ones.
- **Tasks**:
  - priority prefix `h-` `m-` `l-` `r-` `o-` `b-`;
  - the branch derives from the task type (`fix-` → `fix/`, `implement-` → `feature/`, `o-` → `optimize/`, …);
  - edits are blocked on the wrong branch.
- **Code review**:
  - rounds of parallel reviewers (Claude agents + Codex/agy when enabled);
  - findings confirmed by the orchestrator;
  - three-level severity (Critical / Warning / Note);
  - spec-compliance audit last;
  - both stages are bound to the working tree by an engine-stamped hash.
- **Completion**: the provider chain (GitLab / GitHub / Gitea / Jira), or the local menu `merge_local` / `push_pr` / `keep` / `discard` when no tracker is configured.
- **Context**: auto-compaction at 85% (configurable); PreCompact saves task/branch/protocol/DAIC state and the next prompt restores it.
- **Safety guards**: destructive Bash commands need a typed `SAFETY_CONFIRM=<nonce>` re-run — friction, not security.
- **Wiki**: `wiki.enabled` turns on the LLM wiki workflow; `wiki/` is editable in every DAIC mode.

## Configuration (`team-management/config.json`)
Edited through `/team-management:config`, which validates against `_CONFIG_SCHEMA` in `plugin/mcp/tools/config.py`. Tokens never go there: they live in the per-project `.claude/state/provider-tokens.json` (`0600`, user-authored).
- Identity: `developer_name`, `project_name` (statusline label; falls back to the folder name).
- Workflow:
  - `protocol_engine.enabled`, `blocked_tools`;
  - `branch_enforcement.enabled`, `branch_enforcement.branch_prefixes`;
  - `task_detection.enabled`.
- Issue tracking:
  - `issue_tracking.provider` (`gitlab` | `jira` | `github` | `disabled`), `issue_tracking.auto_sync`;
  - per provider (`gitlab.*`, `jira.*`, `github.*`): `enabled`, `base_url` (https only), the project/repository key, `auto_sync`, `issue_tracking_enabled`, labels.
  - Gitea uses the `github.*` keys and is detected from `base_url`.
- AI providers:
  - `ai_providers.enabled_providers`;
  - six `ai_providers.include_in_<phase>` flags;
  - `codex.enabled`, `agy.enabled`.
  - `ai_providers.timeout` is not read by the wrappers.
- Code review:
  - `code_review.enforce_warnings` — strict mode: confirmed Warnings block;
  - `code_review.{testing_specialist,red_team,cleanliness}_enabled` — gates for the classifier-dispatched specialists;
  - `test_command` — an allowlisted test runner for the `verify_tests_pass` gate.
- Documentation: `documentation.claude_md_hygiene`, `documentation.claude_md_max_lines` (default 300).
- Context: `auto_compact.enabled`, `auto_compact.threshold`, `auto_compact.context_limit`.
- Safety: `safety_guards.enabled`, `safety_guards.sensitivity`, `safety_guards.allowlist`.
- `notifications.*`, `wiki.enabled`, `features.icon_style`.

## Writing CLAUDE.md Files
These files are re-read into every session.
- Keep: module purpose, key files one line each, non-obvious invariants and pitfalls, config keys.
- Leave out: task names, review results, history wording, rationale narratives.
  - Rationale goes to `wiki/` decision pages; changes go to `plugin/CHANGELOG.md`; history lives in git.
- Each file stays under 300 lines. `test/test_claude_md_hygiene.py` checks these repository files in full.

## team-management Behaviors

Behavioral guidance (DAIC, TDD, debugging, code-review discipline) and wiki behavior are plugin-owned and imported through the managed block below. Project-specific rules go in `CLAUDE.tm.custom.md`.

<!-- team-management:begin (managed by /team-management:init; do not edit inside) -->
@CLAUDE.tm.md
@CLAUDE.tm.custom.md
@CLAUDE.wiki.md
<!-- team-management:end -->
