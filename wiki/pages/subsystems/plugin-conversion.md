---
title: Plugin Conversion Architecture
tags: [architecture, mcp, hooks, config, cross-platform, testing]
created: 2026-06-24
updated: 2026-07-06
sources: [plugin/hooks/shared_state.py, plugin/mcp/bootstrap_mcp.py, plugin/hooks/issue_provider_base.py, plugin/hooks/session-start.py, plugin/commands/init.md, plugin/.mcp.json, plugin/hooks/hooks.json, test/test_plugin_api_regression.py]
---

# Plugin Conversion Architecture

team-management ships as a **native Claude Code plugin**. The plugin source lives under `plugin/`; the marketplace descriptor is `.claude-plugin/marketplace.json` → `./plugin`. This page maps the plugin-era runtime: the path model, runtime wiring, the cross-platform launcher, namespacing, and provider-token continuity.

## Why a plugin
A plugin reads a single source from `${CLAUDE_PLUGIN_ROOT}` — no copying of provider utilities into deployed locations, so there is no file-copy version skew between the hooks and the MCP server. It ships hooks + MCP server + commands + agents + protocol configs as one versioned, marketplace-installable unit.

## Three-root path model
The core structural model (`plugin/hooks/shared_state.py`). Each root is resolved at point of use — there is deliberately **no import-time `PLUGIN_ROOT` constant** (it would freeze the env):
- **PROJECT_DIR** — `get_project_root()`, reads `CLAUDE_PROJECT_DIR` first (the user's project: `team-management/config.json`, `.claude/state/`, `tasks/`, `protocol-configs/custom/`, `wiki/`).
- **PLUGIN_ROOT** — `get_plugin_root()`, reads `CLAUDE_PLUGIN_ROOT` (read-only install: hooks src, mcp src, agents, commands, `protocol-configs/` system configs, knowledge, templates). Replaced on every plugin update — never persist state under it.
- **PLUGIN_DATA** — `get_plugin_data()`, reads `CLAUDE_PLUGIN_DATA` **only** (no `__file__` fallback): the venv and caches; survives updates. `None` when unset.

Resolvers that carry a PLUGIN_ROOT tier: `load_protocol_config`, `resolve_protocol_start_text`, `protocol_engine.list_protocols`/`customize_protocol`, `ai_providers._load_provider_template`, `mcp/core/project.get_hooks_path()` — each prefers custom (project) → system (PLUGIN_ROOT) → legacy deployed → inline.

## Runtime wiring
- **Hooks**: `plugin/hooks/hooks.json` registers all events via a stdlib launcher shim `python3 ${CLAUDE_PLUGIN_ROOT}/hooks/_shim.py <hook>`; the shim picks the venv python if it exists, else system python. Pure stdlib so it runs before the venv exists.
- **MCP cold-start**: `plugin/.mcp.json` → `python3 ${CLAUDE_PLUGIN_ROOT}/mcp/bootstrap_mcp.py`. `bootstrap_mcp.py` (stdlib-only) builds/validates a venv under `${CLAUDE_PLUGIN_DATA}/venv` keyed to the sha256 of `requirements.lock` (hash written last → a crashed build self-heals; interprocess build lock), then `os.execv`s the venv python running `server.py` **before** any MCP handshake.
- **Boot-detector**: guards against a legacy pip install coexisting with the plugin (both hook sets firing) — SessionStart advisory + PreToolUse hard block, gated on `is_plugin_mode()`.
- **Behavioral guidance**: `shared_state.ensure_guidance_deployed_and_wired` (called from `session-start.py` and the `config_update` MCP tool) deploys the plugin-owned `CLAUDE.tm.md` / knowledge files (+ `CLAUDE.wiki.md` when wiki is enabled) into the project, **then** wires an idempotent `<!-- team-management:begin … -->` / `<!-- team-management:end -->` managed block into the project-root `CLAUDE.md` that `@`-imports them alongside the user-owned `CLAUDE.tm.custom.md` stub. Because a project-root `CLAUDE.md` and its `@`-imports are re-read after `/compact`, the guidance is durable across long sessions.
- **statusLine**: Claude Code does not expand `${CLAUDE_PLUGIN_ROOT}` in a `settings.json` `statusLine.command`, and plugins cannot declare a native statusLine. `session-start.py::_ensure_statusline_pinned` pins the resolved absolute `templates/statusline.py` path into the gitignored per-machine `.claude/settings.local.json`, using `sys.executable` (the hook's own interpreter) so it resolves on every platform; it re-pins when the plugin version dir changes.
- **init writes**: `/team-management:init` writes `<project>/.claude/settings.json` (plugin-enablement keys) and creates the `<project>/CLAUDE.tm.custom.md` stub. Both targets are on the `sessions-enforce.py` administrative whitelist (exact resolved-path equality), so init writes them directly even while the plugin is enforcing.

### Cross-platform launcher — the `python3` token
The hooks shim and MCP cold-start both invoke the literal token `python3`. It is correct on macOS/Linux and is intentionally kept — there is no single static token that works on both platforms (modern macOS ships only `python3`; Windows ships `python`/`py` but not `python3`), and plugin manifests have no per-OS branch. Windows has no `python3.exe`, so `/team-management:init` §0 (a prompt, so it runs without working hooks/MCP) copies `python.exe` → `python3.exe` into the Python install dir — a real `.exe` found by every launch path (direct-spawn, bash, cmd, PowerShell), ahead of the `%LOCALAPPDATA%\Microsoft\WindowsApps` Store alias. It requires the `py` launcher. The pinned statusLine command uses `sys.executable` (guaranteed present and ≥3.10) rather than a literal `python3` for the same reason, and its self-heal check normalizes path separators so a Windows backslash command re-pins across plugin-version bumps. The user-facing requirement (run `/team-management:init` once on Windows, then fully restart) is documented in `README.md` + `docs/INSTALL.md` with the `python3: command not found` symptom for self-diagnosis.

## Namespacing (two distinct schemes — do not conflate)
- **Slash commands** namespace as `/team-management:<filename-without-.md>` — so command files carry BARE names (8 total: config, init, clean-check, wiki-ingest, wiki-lint, wiki-tune, custom-protocol-create, custom-protocol-update-after-reinstall).
- **MCP tools** are namespaced `mcp__plugin_<plugin-name>_<server-key>__<tool>`, where `<server-key>` is the `plugin/.mcp.json` server KEY. The key is `tm`, so the canonical runtime form is **`mcp__plugin_team-management_tm__<tool>`** (identical for `--plugin-dir` and marketplace installs). A drift-guard (`test/test_mcp_tool_namespace.py`) forbids the bare `mcp__team-management__*` and doubled forms across `plugin/`. `server.py`'s `FastMCP("team-management")` name does not drive the tool prefix.

## Continuity (existing users)
Provider tokens resolve **file → config fallback** via `shared_state.resolve_provider_token`: the tokens live in a per-project, user-authored `0600` `.claude/state/provider-tokens.json` (keyed by provider name; a PROTECTED path the AI cannot read), seeded create-if-absent by `ensure_provider_tokens_file`. The OS-keychain `userConfig` model was retired in `m-per-project-provider-tokens` — it was global-per-plugin-per-user, so two projects could not use different tokens (`plugin.json` no longer declares `userConfig`; there is no env/keychain tier). A pre-existing plaintext `team-management/config.json` still works as the final fallback (and legacy `CLAUDE_PLUGIN_OPTION_*`-keyed bridge files are still read), so no reconfiguration is required.

## Testing
The plugin-API regression suite (`test/test_plugin_api_regression.py`) maps each load-bearing plugin-API fact — the merge-friendly hook manifest, `CLAUDE_PROJECT_DIR`-in-env, the three-root resolvers, the launcher shim, the boot-detector, command namespacing, and token resolution (F5 now asserts the manifest declares NO `userConfig` and that resolution is file→config) — to the contract that guards it, so a future Claude Code release that breaks an assumption fails loudly and names the fact. Two host behaviors it documents but cannot re-prove in pytest: the cross-source hook merge (plugin `hooks.json` merging with the project's `.claude/settings.json`) and `CLAUDE_PROJECT_DIR` injection are owned by Claude Code's loader; the tests pin our side (manifest structure, env-first resolvers, ordering).
