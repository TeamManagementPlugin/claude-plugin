# team-management

**team-management framework** - A complete workflow management system that transforms Claude Code from a basic AI assistant into a structured, persistent development environment with multi-provider issue tracking integration.

## Overview

team-management enforces the **DAIC (Discussion-Alignment-Implementation-Check)** methodology through Python hooks that cannot be bypassed. When Claude attempts to edit code without explicit user approval, the system blocks the tools and requires discussion first. This prevents over-implementation and ensures collaborative alignment.

**Key Capabilities**:
- **DAIC Enforcement**: Mandatory discussion before code changes via hook-based tool blocking
- **Persistent Tasks**: Maintain task context across sessions with automatic loading
- **Branch Enforcement**: Prevents wrong-branch commits through git branch validation
- **Multi-Provider Integration**: Full bidirectional sync with GitLab, Jira, GitHub, or Gitea
- **MCP Server**: First-class tool exposure for guaranteed agent discoverability
- **Specialized Agents**: Context gathering, code review, logging, and more

## Installation

team-management is a **native Claude Code plugin** — install it through Claude Code's
plugin/marketplace system (there is no pip/npm package and no separate installer to run):

```
/plugin marketplace add TeamManagementPlugin/claude-plugin
```
```
/plugin install team-management@team-management
```

(`TeamManagementPlugin/claude-plugin` is the GitHub marketplace repo — a full URL or a local checkout path also works.)

Then enable it for the project (and your team) and restart Claude Code:

```
/team-management:init
```

> **Windows — this step is required, not optional.** The plugin's hooks and MCP server launch via the `python3` command, which Windows doesn't provide (only `python.exe` and the `py` launcher). `/team-management:init` provisions a real `python3.exe` for you — it uses the `py` launcher to find your Python, then copies `python.exe` → `python3.exe` — so run it once and then **fully quit and reopen Claude Code**. Skip it and the plugin won't load: you'll see `python3: command not found` (or `'python3' is not recognized as an internal or external command`). The `py` launcher ships with the python.org installer.

On first use the plugin's MCP server cold-starts and builds its own isolated venv — the
tools appear once it connects. Configure non-secret settings with `/team-management:config`,
and put provider tokens in the per-project file `.claude/state/provider-tokens.json` — a
git-ignored, owner-only (`0600`) file that Claude cannot read (a protected path). It is
auto-created with blank provider keys the first time you run `/team-management:config` or
start a session; open it in your editor and fill in only the tokens you use.

**Developing from a checkout:** run `claude --plugin-dir /path/to/team-management/plugin`,
or register the checkout as a local marketplace with
`/plugin marketplace add /path/to/team-management`.

See **[docs/INSTALL.md](docs/INSTALL.md)** for prerequisites, team-wide enablement,
dev-mode, and uninstall (including the data-loss guard around the `team-management/`
directory).

## Quick Start

**First 5 minutes with team-management:**

1. **Install** (see above) and **restart Claude Code** so the plugin's hooks and MCP server load.

2. **Describe what you want to work on**, in plain language — for example, asking to add user authentication to the API.

   Claude won't start coding. Instead it recommends a workflow protocol (the standard one is the `task` protocol), explains why it fits, and asks for your go-ahead.

3. **Approve the protocol.** Claude starts it (`protocol_start`). From here the protocol drives the lifecycle and manages DAIC mode, the git branch, the task file, and issue tracking (if a provider is configured) automatically at every step:

   - **Investigation** _(discussion mode, read-only)_ — Claude explores the codebase, clarifies scope, and presents a plan with measurable success criteria.
   - **Implementation** _(implementation mode)_ — once you approve the plan, the protocol creates the branch and task file and unblocks editing; Claude writes code and tests.
   - **Code review** _(implementation mode)_ — spec-compliance and code-quality review (plus any enabled AI providers); must pass before advancing.
   - **Documentation** _(documentation mode, docs-only)_ — CLAUDE.md / docs updates and work-log finalization.
   - **Completion** _(discussion mode)_ — you test the change and confirm; the protocol then wraps up the git and issue work. With an issue-tracking provider configured: commit, push, open the MR/PR, sync the linked issue, and archive. With issue tracking disabled (the default): it offers a completion menu — merge locally / push + PR / keep / discard.

4. **Your touchpoints.** You stay in control at every boundary — you approve the plan before implementation begins, and Claude advances a step (`protocol_advance`) only once you're satisfied, so nothing runs ahead. Each step runs in its own DAIC mode, which the protocol sets on entry — it doesn't drop back to discussion between steps (code review, for example, stays in implementation mode), so you never toggle modes by hand.

5. **Finish.** At the completion step you verify the change works and confirm, and the protocol wraps everything up — the exact git and issue actions depend on your issue-tracking config and completion choice (see the Completion row above).

**Other protocols** — Claude picks the fit and explains why before starting:
- `task` — standard change lifecycle (above)
- `research` — spikes, PoCs, evaluations
- `brainstorm` — parallel-specialist ideation
- `refactoring` — test-baseline-gated restructuring
- `optimize` / `optimize-unattended` — metric-driven optimization loops

> **Manual DAIC override:** the protocol manages discussion/implementation/documentation mode for you, so you rarely switch by hand. The `mcp__plugin_team-management_tm__daic_mode_switch_*` tools remain available if you ever need a manual override.

## Three-Provider Issue Tracking

team-management provides full bidirectional integration with three issue tracking systems:

### GitLab

**Features**:
- Issues and Merge Requests API
- Auto-close issues on MR merge
- Label inheritance (issue → MR)
- Complete CI/CD workflow automation
- Rich Markdown formatting

**Configuration** (team-management/config.json):
```json
{
  "issue_tracking": {
    "provider": "gitlab"
  },
  "gitlab": {
    "enabled": true,
    "api_token": "glpat-your-token",
    "base_url": "https://gitlab.com",
    "project_path": "namespace/project",
    "auto_sync": true,
    "default_labels": ["claude-code", "automated"]
  }
}
```

**Capabilities**:
- Create GitLab issues from Claude tasks
- Import GitLab issues as Claude tasks
- Auto-sync task status → issue state
- Create merge requests linked to issues
- Complete workflow: commit → push → MR → close issue

### Jira

**Features**:
- Full Jira REST API v2/v3 integration
- Markdown-to-Jira-Wiki conversion
- Issue type support (Bug, Epic, Story, Task, Sub-task)
- Workflow state transitions
- Personal Access Token authentication

**Configuration**:
```json
{
  "issue_tracking": {
    "provider": "jira"
  },
  "jira": {
    "enabled": true,
    "api_token": "your-personal-access-token",
    "base_url": "https://company.atlassian.net",
    "project_key": "PROJ",
    "auto_sync": true,
    "default_issue_type": "Task"
  }
}
```

**Capabilities**:
- Create Jira issues from Claude tasks
- Import Jira issues as Claude tasks
- Auto-sync task status → Jira transitions
- Support all Jira issue types
- Rich comments with wiki markup

### GitHub / Gitea

**Features**:
- Issues and Pull Requests API
- Label-based workflow management
- GitHub-Flavored Markdown support
- GitHub Enterprise and Gitea compatibility
- Auto-close issues on PR merge

**Gitea Compatibility**: Gitea implements GitHub's API, so the GitHub integration works seamlessly with Gitea instances. Simply configure the base_url to point to your Gitea server.

**Configuration**:
```json
{
  "issue_tracking": {
    "provider": "github"
  },
  "github": {
    "enabled": true,
    "api_token": "ghp_your-token",
    "base_url": "https://api.github.com",
    "repository": "owner/repo",
    "auto_sync": true,
    "default_labels": ["claude-code", "automated"],
    "workflow_labels": {
      "in_progress": "in-progress",
      "blocked": "blocked",
      "pending": "pending"
    }
  }
}
```

**Gitea Configuration Example**:
```json
{
  "issue_tracking": {
    "provider": "github"
  },
  "github": {
    "enabled": true,
    "api_token": "your-gitea-access-token",
    "base_url": "https://git.example.com/api/v1",
    "repository": "team/project",
    "auto_sync": true,
    "default_labels": ["claude-code", "automated"],
    "workflow_labels": {
      "in_progress": "in-progress",
      "blocked": "blocked",
      "pending": "pending"
    }
  }
}
```

**Capabilities**:
- Create GitHub/Gitea issues from Claude tasks
- Import GitHub/Gitea issues as Claude tasks
- Auto-sync task status → issue state + labels
- Create pull requests linked to issues
- Complete workflow: commit → push → PR → close issue

**Status Mapping**: Both GitHub and Gitea have only 2 states (open/closed), workflow states managed via labels.

## Core Features

### DAIC Enforcement

**Discussion mode blocks implementation tools** until the workflow protocol advances or you explicitly approve:

- A workflow protocol drives DAIC mode per step: investigation runs in discussion mode (read-only), implementation unblocks editing, documentation allows docs-only edits.
- In discussion mode Claude proposes an approach and investigates the codebase, but cannot edit.
- Advancing the protocol (`protocol_advance`), after you approve the step, switches to the next step's mode automatically — for example, investigation to implementation unblocks editing.
- Each advance is user-gated: Claude calls `protocol_advance` only after you approve, so the workflow never runs ahead — but the new mode is the next step's, not necessarily discussion (e.g. code review stays in implementation mode).
- Manual override: the `mcp__plugin_team-management_tm__daic_mode_switch_*` tools switch mode by hand if you ever need to step outside the protocol.

**How it works**: Python hooks intercept tool usage and check `.claude/state/daic-mode.json`. If mode is "discussion" and tool is Edit/Write, the hook blocks execution and shows error message.

**Cannot be bypassed** - hooks execute before tools, enforcement is absolute.

### Persistent Task Management

**Task files** (team-management/tasks/):
- Markdown with YAML frontmatter
- Priority prefixes: `h-` (high), `m-` (medium), `l-` (low), `r-` (research/investigate), `o-` (optimize), `b-` (brainstorm)
- Automatic context loading on session start
- Work log consolidation by logging agent
- Context manifests by context-gathering agent

**Example task file**:
```markdown
---
task: h-implement-auth
branch: feature/h-implement-auth
status: in-progress
created: 2025-11-04
modules: [auth-service]
---

# Implement User Authentication

## Problem/Goal
...

## Success Criteria
- [ ] JWT token generation
- [ ] Refresh token flow
...

## Work Log
- [2025-11-04] Started implementation...
```

### Branch Enforcement

Task naming automatically determines required git branch:

- `fix-*` → `fix/*` branch
- `implement-*` → `feature/*` branch
- `experiment-*` → `experiment/*` branch

**Enforcement**: Hooks block Edit/Write tools if current branch doesn't match task requirement. Prevents accidental commits to wrong branch.

### Auto-Sync

When enabled, task status changes automatically sync to linked issues:

- Edit task file status field
- Post-tool-use hook detects change
- Updates provider issue with status + comment
- Maintains sync metadata and timestamps

### AI Provider Integration

team-management supports external AI providers (Codex, Antigravity) for multi-model code analysis across six protocol phases (task investigation, implementation, code review, brainstorm analysis, research exploration, and refactoring planning).

**How it works**: each enabled provider runs as a parallel **Task agent** (not an MCP server) alongside Claude during the configured phase. Output is advisory — provider failures degrade gracefully and never block the workflow.

#### OpenAI Codex

**Prerequisites**:
```bash
npm install -g @openai/codex
codex auth login
```

**Configuration** (team-management/config.json):
```json
{
  "ai_providers": {
    "enabled_providers": ["codex"],
    "include_in_code_review": true
  },
  "codex": {
    "enabled": true
  }
}
```

#### Google Antigravity CLI

**Prerequisites**: Antigravity CLI installed and authenticated (run `agy` once to sign in).

> **Experimental**: verified on macOS only — `--sandbox` behaviour on Linux/Windows is unverified, so this provider is opt-in; enable it via `/team-management:config`.

**Configuration**:
```json
{
  "ai_providers": {
    "enabled_providers": ["agy"],
    "include_in_code_review": true
  },
  "agy": {
    "enabled": true
  }
}
```

agy uses the CLI's default model — the framework does not override it. The wrapper runs `agy --sandbox` (read-only); if agy's own `write_file` tool mutates the tree, the wrapper detects the change via a `git status` snapshot and prepends a warning (detect & report, never auto-revert).

#### Workflow Integration

AI providers participate in six protocol phases, each gated by a config key:
- **Code Review** (`include_in_code_review`) — parallel security/quality review of the diff
- **Task Investigation** (`include_in_investigation`) — independent reading of task scope and risks
- **Task Implementation** (`include_in_implementation`) — plan review before code is written
- **Brainstorm Analysis** (`include_in_brainstorm`) — analysis alongside the specialist agents
- **Research Exploration** (`include_in_research_exploration`) — independent exploration of the research question
- **Refactoring Planning** (`include_in_refactoring_planning`) — review of the refactoring plan

`ai_providers.timeout` (default `300`) is currently **inert** — the wrappers enforce a fixed deadline (codex 300s, agy 330s watchdog) and do not read this key; it is kept for a future plumbing task. See [AI Provider Phase Coverage](docs/USAGE_GUIDE.md#ai-provider-phase-coverage) for the full step-by-step mapping and the [Custom AI Provider Prompt Templates](docs/USAGE_GUIDE.md#custom-ai-provider-prompt-templates) section for per-project prompt overrides.

**How it works**: each enabled provider runs as a parallel **Task agent** (not an MCP server) using a wrapper prompt at `plugin/agents/codex-cli.md` or `plugin/agents/agy-cli.md`. The wrappers invoke `codex review --uncommitted` / `codex exec -s read-only` or `agy --sandbox --print-timeout 300s -p ...` in their own context window with full read-only access to the repository — so providers can explore the codebase rather than being limited to a pre-supplied diff. Enable and configure providers via `/team-management:config`.

## MCP Server

The **Model Context Protocol** server exposes issue tracking as first-class Claude Code tools.

**Why MCP?** Claude agents reliably discover MCP tools in the tool list, but often miss slash commands.

**Installation**: bundled with the plugin — the server is registered by `plugin/.mcp.json` and cold-starts its own venv on first load (nothing to install separately).

**Available Tools**:
- `mcp__plugin_team-management_tm__issue_status` - Show provider config and linked tasks
- `mcp__plugin_team-management_tm__issue_read` - Import issue as task
- `mcp__plugin_team-management_tm__issue_create` - Create issue from task
- `mcp__plugin_team-management_tm__issue_update` - Update issue title/description/status/labels
- `mcp__plugin_team-management_tm__issue_sync` - Sync task status to issue
- `mcp__plugin_team-management_tm__issue_link` - Link task to existing issue
- `mcp__plugin_team-management_tm__issue_unlink` - Remove issue link

The `task` protocol's completion step handles the full commit → push → MR/PR → archive flow
automatically (no dedicated MCP tool to call).

**Configuration**: registered automatically by the plugin (`plugin/.mcp.json`) — no manual `.mcp.json` editing required.

## Configuration

Configuration happens **inside Claude Code** — there is no installer:

- `/team-management:config` — guided flow that writes non-secret settings to
  `team-management/config.json` (developer name, DAIC options, statusline,
  auto-compact, code-review enforcement, issue-tracking provider, AI providers, LLM wiki).
- `.claude/state/provider-tokens.json` — provider tokens (GitLab / Jira / GitHub / Telegram),
  keyed by provider name. This is a **per-project, user-authored** file: git-ignored,
  owner-only (`0600`), and unreadable by Claude (a protected path). It is auto-created with
  blank keys plus an explanatory `_comment` the first time you run `/team-management:config`
  or start a session — open it in your editor and fill in only the tokens you use. Because
  each project has its own file, different projects can use different tokens. Tokens never
  enter `config.json` or the chat transcript (a token already present in `config.json` from a
  legacy install still works as a fallback).
- `/team-management:init` — enables the plugin for the project by merging `enabledPlugins` /
  `extraKnownMarketplaces` into `.claude/settings.json` (commit those so your team auto-enables).
  It does NOT write a `statusLine` — the SessionStart hook pins the resolved statusline path into
  the gitignored `.claude/settings.local.json`.

The plugin reads/writes your project's `team-management/` directory (tasks, config, custom
protocols) and `.claude/state/` (managed runtime state).

## Commands

**Slash Commands** (in Claude Code, namespaced `/team-management:*`):
- `/team-management:config` - Configure non-secret settings
- `/team-management:init` - Enable the plugin for the project
- `/team-management:wiki-ingest` / `:wiki-lint` / `:wiki-tune` - LLM wiki operations
- `/team-management:clean-check`, `:custom-protocol-create`, `:custom-protocol-update-after-reinstall`

**MCP Tools** (in Claude Code):
- `mcp__plugin_team-management_tm__issue_status` - Check provider configuration
- `mcp__plugin_team-management_tm__issue_read` - Import issue as task
- `mcp__plugin_team-management_tm__issue_create` - Create issue from task
- `mcp__plugin_team-management_tm__issue_sync` - Sync task status

## Documentation

- **[MCP Server Guide](docs/MCP_SERVER.md)** - MCP architecture and tool reference
- **[CLAUDE.md](CLAUDE.md)** - Project architecture and patterns
- **[CLAUDE.tm.md](plugin/templates/CLAUDE.tm.md)** - Collaboration philosophy

## Provider Setup

### Getting API Tokens

Once you have a token, add it to the per-project `.claude/state/provider-tokens.json` file
(keyed by provider name: `gitlab` / `jira` / `github` / `telegram`) — git-ignored,
owner-only, and unreadable by Claude. Tokens never go into `config.json` or the chat
transcript.

**GitLab**:
1. Go to GitLab → Preferences → Access Tokens
2. Create token with `api` scope
3. Copy token (starts with `glpat-`) into the `gitlab` key of `provider-tokens.json`

**Jira**:
1. Go to Jira → Profile → Personal Access Tokens
2. Create token with project access
3. Copy token into the `jira` key of `provider-tokens.json`

**GitHub**:
1. Go to GitHub → Settings → Developer settings → Personal access tokens
2. Generate new token (classic) with `repo` scope
3. Copy token (starts with `ghp_`) into the `github` key of `provider-tokens.json`

### Configuration Examples

See `plugin/templates/config.template.json` for complete configuration examples with all available options.

## Troubleshooting

**MCP tools not appearing**:
- On first load the MCP server is still cold-start-building its venv — the tools appear the
  turn after it connects; give it a moment and retry
- Confirm the plugin is enabled: run `/plugin` and check team-management is installed
- Fully restart Claude Code (quit and reopen)

**Hook not blocking edits**:
- Confirm the plugin is enabled (`/plugin`) and you've restarted Claude Code since enabling
- Run `/team-management:init` so the project's `.claude/settings.json` enables the plugin for the team
- Check `.claude/state/daic-mode.json` shows the expected mode
- Fully restart Claude Code

**Provider sync failing**:
- Run `mcp__plugin_team-management_tm__issue_status` to check configuration
- Verify API token is valid
- Check network connectivity to provider
- Review error in terminal/logs

## Development

**Testing Changes**:

```bash
# Edit the plugin source under plugin/
vim plugin/hooks/sessions-enforce.py

# Load the checkout directly and test in Claude Code
claude --plugin-dir /path/to/team-management/plugin

# Run the test suite
python3 -m pytest test/
```

**Key Files for Contributors**:
- `plugin/hooks/*.py` - Hook implementations + protocol engine
- `plugin/mcp/server.py` - MCP server (cold-start via `plugin/mcp/bootstrap_mcp.py`)
- `plugin/.mcp.json`, `plugin/hooks/hooks.json` - plugin runtime wiring

## Requirements

- **Python**: 3.10+ (the plugin builds its own isolated venv on first run)
- **Git**: Recommended for branch enforcement
- **Claude Code**: Required (loads the plugin's hooks + MCP server)

The plugin's runtime dependencies (`mcp`, `tiktoken`, `requests`) are installed into a
plugin-private venv automatically on cold start — you do not install them yourself.

## License

MIT License - See LICENSE file for details.

## Credits

team-management stands on the shoulders of projects and ideas that shaped its design:

- **[cc-sessions](https://github.com/GWUDCAP/cc-sessions)** by GWUDCAP — origin of the
  **DAIC (Discussion-Alignment-Implementation-Check)** methodology and the sessions/hook-enforcement
  model team-management is built on.
- **[superpowers](https://github.com/obra/superpowers)** by Jesse Vincent (obra) — a composable-skills
  methodology for coding agents; inspiration for the skill/protocol-driven workflow.
- **[get-shit-done](https://github.com/open-gsd/gsd-core)** GSD Core — meta-prompting, context
  engineering, and spec-driven development for AI coding agents.
- **[LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)** by Andrej Karpathy —
  the persistent, compounding AI-maintained knowledge-base pattern behind the **LLM Wiki** feature.
- **[autoresearch](https://github.com/karpathy/autoresearch)** by Andrej Karpathy — autonomous,
  metric-driven overnight experimentation that inspired the **optimize** / **optimize-unattended** protocols.

---

**Links**:
- [MCP Server Documentation](docs/MCP_SERVER.md)
